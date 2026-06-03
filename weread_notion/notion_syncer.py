"""
Notion 同步模块
负责在 Notion 中创建/更新数据库和页面
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from notion_client import Client
from notion_client.errors import APIResponseError

from weread_notion.common import GITHUB_COVER_BASE, seconds_to_hm, retry_on_failure

logger = logging.getLogger(__name__)

# 封面存放目录
COVERS_DIR = Path("covers")


# ── Notion 富文本块辅助 ──────────────────────────────────────────────────────

def _text(content: str, bold: bool = False, color: str = "default", url: str = "") -> dict:
    """生成 rich_text 对象，可选超链接"""
    annotations = {"bold": bold, "color": color}
    obj = {
        "type": "text",
        "text": {"content": content[:2000]},  # Notion 单段限 2000 字符
        "annotations": annotations,
    }
    if url:
        obj["text"]["link"] = {"url": url}
    return obj


def _rich(content: str, bold: bool = False) -> list[dict]:
    """生成 rich_text 数组（自动分割超长文本）"""
    if not content:
        return [_text("")]
    chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
    return [_text(c, bold) for c in chunks]



def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(text)}}


def _callout(text: str, emoji: str = "💡", color: str = "gray_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rich(text), "icon": {"type": "emoji", "emoji": emoji}, "color": color}}


def _callout_green(text: str) -> dict:
    """绿色 callout — 自己的划线"""
    return _callout(text, "✏️")


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _persist_cover(book_id: str, weread_cover_url: str, timeout: int = 5) -> str:
    """
    下载微信读书封面并保存到 covers/ 目录。
    返回 GitHub raw URL（用于 Notion 封面）。
    下载失败返回空字符串，不抛异常。
    """
    if not weread_cover_url:
        return ""

    cover_path = COVERS_DIR / f"{book_id}.jpg"

    # 如果本地已有封面，直接返回 GitHub raw URL
    if cover_path.exists():
        return f"{GITHUB_COVER_BASE}/{book_id}.jpg"

    try:
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        resp = requests.get(weread_cover_url, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        image_bytes = resp.content

        if len(image_bytes) < 100:
            return ""

        with open(cover_path, "wb") as f:
            f.write(image_bytes)

        return f"{GITHUB_COVER_BASE}/{book_id}.jpg"
    except Exception:
        return ""


def _h2_colored(text: str, color: str) -> dict:
    """带颜色的 H2 标题（块级 + 富文本注解双重设色）"""
    rich_text = [{
        "type": "text",
        "text": {"content": text},
        "annotations": {"bold": True, "color": color},
    }]
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": rich_text, "color": color},
    }


# ── NotionSyncer ─────────────────────────────────────────────────────────────

class NotionSyncer:
    """
    负责将微信阅读数据写入 Notion。

    Notion 结构设计：
    ┌─ 父页面（用户指定）
    │  ├─ 📚 微信阅读书架（数据库）── 每本书一条记录
    │  │     属性：书名, 作者, 分类, 进度, 阅读时长, 完成状态, 最近阅读, 笔记数, 评分
    │  │     子页面：划线 & 想法（按章节分组）
    """
    SHELF_DB_TITLE = "📚 微信阅读书架"

    # 跨线程 Notion API 限流（≤3 req/s）
    _n_lock = threading.Lock()
    _n_last = 0.0

    def __init__(self, token: str, parent_page_id: str, book_pages: Optional[dict] = None):
        self.client = Client(auth=token)
        self.parent_page_id = parent_page_id
        self._shelf_db_id: Optional[str] = None
        self._book_pages: dict = book_pages or {}

    def _n(self, fn, *args, **kwargs):
        """
        带限流的 Notion API 调用：
        1. 全局锁确保单线程访问
        2. 0.2s 间隔 ≈ 5 req/s（序列化后不会触发 429）
        3. 429/5xx 自动重试（指数退避，最长等 60s）
        """
        with NotionSyncer._n_lock:
            now = time.time()
            elapsed = now - NotionSyncer._n_last
            if elapsed < 0.2:
                time.sleep(0.35 - elapsed)
            NotionSyncer._n_last = time.time()

        attempt = 0
        max_retries = 3
        while True:
            try:
                return fn(*args, **kwargs)
            except APIResponseError as e:
                status = getattr(e, "status", 0) or getattr(e.response, "status_code", 0) if hasattr(e, "response") else 0
                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = min(1.0 * (2 ** attempt), 60.0)
                    attempt += 1
                    logger.warning(f"Notion HTTP {status}, 重试 {attempt}/{max_retries} (等待 {delay:.0f}s)...")
                    time.sleep(delay)
                    continue
                raise
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status in (429, 500, 502, 503, 504) and attempt < max_retries:
                    delay = min(1.0 * (2 ** attempt), 60.0)
                    attempt += 1
                    logger.warning(f"HTTP {status}, 重试 {attempt}/{max_retries} (等待 {delay:.0f}s)...")
                    time.sleep(delay)
                    continue
                raise

    # ── 初始化结构 ──────────────────────────────────────────────────────────

    def setup(self):
        """初始化 Notion 结构（幂等，已存在则跳过）"""
        self._shelf_db_id = self._get_or_create_shelf_db()
        self._ensure_shelf_db_properties()

    def _ensure_shelf_db_properties(self):
        """补充书籍数据库中可能缺失的新字段"""
        try:
            self._n(self.client.databases.update,
                database_id=self._shelf_db_id,
                properties={
                    "阅读时长": {"rich_text": {}},
                    "开始日期": {"date": {}},
                },
            )
        except Exception:
            pass

    def _search_in_parent(self, title: str, obj_type: str) -> Optional[str]:
        """在父页面中查找已存在的子页面/数据库（跳过已归档的）"""
        result = self._n(self.client.search, query=title)
        for item in result.get("results", []):
            if item.get("object") != obj_type:
                continue
            if item.get("archived", False):
                continue
            parent = item.get("parent", {})
            if parent.get("page_id", "").replace("-", "") == self.parent_page_id.replace("-", ""):
                return item["id"]
        return None

    def _get_or_create_shelf_db(self) -> str:
        """获取或创建书架数据库（作为父页面的子数据库）"""
        db_id = self._search_in_parent(self.SHELF_DB_TITLE, "database")
        if db_id:
            return db_id

        db = self._n(self.client.databases.create,
            parent={"type": "page_id", "page_id": self.parent_page_id},
            title=[{"type": "text", "text": {"content": self.SHELF_DB_TITLE}}],
            icon={"type": "emoji", "emoji": "📚"},
            properties={
                "书名": {"title": {}},
                "作者": {"rich_text": {}},
                "分类": {"select": {}},
                "阅读进度": {"number": {"format": "percent"}},
                "完成状态": {
                    "select": {
                        "options": [
                            {"name": "✅ 已读完", "color": "green"},
                            {"name": "📖 阅读中", "color": "blue"},
                            {"name": "📥 未开始", "color": "gray"},
                        ]
                    }
                },
                "评分": {"number": {"format": "number"}},
                "划线数": {"number": {"format": "number"}},
                "想法数": {"number": {"format": "number"}},
                "阅读时长": {"rich_text": {}},
                "开始日期": {"date": {}},
                "最近阅读": {"date": {}},
                "出版社": {"rich_text": {}},
                "ISBN": {"rich_text": {}},
                "豆瓣链接": {"url": {}},
                "微信读书链接": {"url": {}},
                "封面": {"files": {}},
            },
        )
        return db["id"]

    # ── 书架同步 ────────────────────────────────────────────────────────────

    def _find_book_page(self, book_id: str) -> Optional[str]:
        """在书架数据库中按 bookId 查找已有记录（先查本地缓存，再验证 Notion）"""
        notion_id = self._book_pages.get(book_id)
        if notion_id:
            try:
                self._n(self.client.pages.retrieve, page_id=notion_id)
                return notion_id
            except Exception:
                del self._book_pages[book_id]
        return None

    def sync_book(self, book_info, progress_info=None, notebook_info=None, book_shelf=None, book_read_detail=None):
        """同步单本书到书架数据库（创建或更新），返回 Notion 页面 ID"""
        book_id = book_info.get("bookId", "")
        title = book_info.get("title", "未知书名")
        author = book_info.get("author", "")
        cover_url = book_info.get("cover", "")
        category = book_info.get("category", "")
        publisher = book_info.get("publisher", "")
        isbn = book_info.get("isbn", "")
        rating = book_info.get("newRating")

        progress_val = 0
        finish_status = "📥 未开始"
        last_read_date = None
        reading_hours = 0
        start_date = None
        if progress_info and progress_info.get("book"):
            p = progress_info["book"]
            progress_val = p.get("progress", 0) / 100.0
            ut = p.get("updateTime", 0)
            if ut:
                last_read_date = datetime.fromtimestamp(ut).strftime("%Y-%m-%d")
            if p.get("progress", 0) == 100:
                finish_status = "✅ 已读完"
            elif p.get("isStartReading"):
                finish_status = "📖 阅读中"

            for field in ["readTime", "readingTime", "totalReadTime", "duration"]:
                rt = p.get(field, 0)
                if rt and rt > 0:
                    reading_hours = round(rt / 3600, 1)
                    break

            for field in ["firstReadTime", "firstOpenTime", "createTime"]:
                ft = p.get(field, 0)
                if ft and ft > 0:
                    start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                    break

        if not start_date:
            for field in ["createTime", "addTime", "create_time", "add_time"]:
                ft = book_info.get(field, 0)
                if ft and ft > 0:
                    start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                    break

        if not start_date and book_shelf:
            for field in ["createTime", "addTime", "readUpdateTime"]:
                ft = book_shelf.get(field, 0)
                if ft and ft > 0:
                    start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                    break

        if not start_date and book_read_detail:
            for field in ["firstReadTime", "firstOpenTime", "createTime", "startTime"]:
                ft = book_read_detail.get(field, 0)
                if ft and ft > 0:
                    start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                    break

        highlight_count = notebook_info.get("noteCount", 0) if notebook_info else 0
        review_count = notebook_info.get("reviewCount", 0) if notebook_info else 0

        weread_url = f"weread://reading?bId={book_id}"
        properties = {
            "书名": {"title": [{"type": "text", "text": {"content": title}}]},
            "作者": {"rich_text": _rich(author)},
            "分类": {"select": {"name": category or "其他"}},
            "阅读进度": {"number": progress_val},
            "完成状态": {"select": {"name": finish_status}},
            "划线数": {"number": highlight_count},
            "想法数": {"number": review_count},
            "微信读书链接": {"url": weread_url},
        }
        if publisher:
            properties["出版社"] = {"rich_text": _rich(publisher)}
        if isbn:
            properties["ISBN"] = {"rich_text": _rich(isbn)}
        if rating is not None:
            properties["评分"] = {"number": round(rating / 10, 1)}
        if last_read_date:
            properties["最近阅读"] = {"date": {"start": last_read_date}}
        if reading_hours > 0:
            properties["阅读时长"] = {"rich_text": _rich(f"{reading_hours}h")}
        if start_date:
            properties["开始日期"] = {"date": {"start": start_date}}

        existing_id = self._find_book_page(book_id)
        if existing_id:
            update_props = {k: v for k, v in properties.items() if k != "书名"}
            self._n(self.client.pages.update, page_id=existing_id, properties=update_props)
            return existing_id
        else:
            page = self._n(self.client.pages.create,
                parent={"database_id": self._shelf_db_id},
                properties=properties,
                icon={"type": "emoji", "emoji": "📖"},
            )
            notion_id = page["id"]
            self._book_pages[book_id] = notion_id
            return notion_id

    def delete_book_page(self, notion_page_id: str, book_id: str = "") -> bool:
        """从 Notion 中删除（归档）一本书的页面，同时从本地缓存中移除映射"""
        try:
            self._clear_page_content(notion_page_id)

            def _archive():
                self._n(self.client.pages.update, page_id=notion_page_id, archived=True)
            retry_on_failure(_archive, max_retries=2, base_delay=0.5)

            if book_id and book_id in self._book_pages:
                del self._book_pages[book_id]
            return True
        except Exception:
            if book_id and book_id in self._book_pages:
                del self._book_pages[book_id]
            return False

    def update_book_cover(self, notion_page_id: str, github_cover_url: str) -> bool:
        """更新单本书的页面封面 + 数据库"封面" files 属性（带重试）"""
        if not github_cover_url:
            return False
        try:
            def _update():
                self._n(self.client.pages.update,
                    page_id=notion_page_id,
                    cover={"type": "external", "external": {"url": github_cover_url}},
                    properties={
                        "封面": {
                            "files": [{
                                "name": "cover.jpg",
                                "type": "external",
                                "external": {"url": github_cover_url},
                            }]
                        }
                    },
                )
            retry_on_failure(_update, max_retries=2, base_delay=0.5)
            return True
        except Exception:
            return False

    def update_page_covers(self, max_workers: int = 10) -> int:
        """并发更新 Notion 页面封面"""
        updated = 0
        tasks = []
        for book_id, page_id in self._book_pages.items():
            cover_path = COVERS_DIR / f"{book_id}.jpg"
            if cover_path.exists():
                url = f"{GITHUB_COVER_BASE}/{book_id}.jpg"
                tasks.append((book_id, page_id, url))
        if not tasks:
            return 0

        def _update(page_id: str, url: str) -> bool:
            return self.update_book_cover(page_id, url)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_update, pid, url): bid for bid, pid, url in tasks}
            for future in as_completed(futures):
                if future.result():
                    updated += 1
        return updated

    # ── 重写书籍子页面（新结构） ──────────────────────────────────────────

    def sync_book_notes(self, page_id, notes_data, social_data=None, book_title="",
                        book_info=None, book_read_detail=None, progress_info=None,
                        book_id="", start_date=""):
        """
        重写书籍子页面，5 个 h2 模块（黄色背景），章节标题 h3（绿色背景）。
        保留用户在 书籍简介 和 启迪思考 中自行填写的内容。

        ⚠ 关键安全策略：先构建 blocks，确认无误后再清空页面。
          避免"清空后写入失败导致页面空白"的问题。
        """
        highlights = notes_data.get("highlights", [])
        chapters_map = notes_data.get("chapters", {})
        reviews = notes_data.get("reviews", [])
        social = (social_data or {}).get("social", {})

        from collections import defaultdict

        # 划线按章节分组，按内容位置排序
        ch_my_hl = defaultdict(list)
        for hl in sorted(highlights, key=lambda h: (h.get("chapterUid", 0), h.get("range", ""))):
            ch_my_hl[hl.get("chapterUid", 0)].append(hl)

        # 想法关联 & 按章节拆分评价
        review_by_abstract = {}
        ch_reviews = defaultdict(list)   # {chapterUid: [review]}
        book_reviews = []                # 整本书评价（无章节归属）
        for rv_item in reviews:
            rv = rv_item.get("review", {})
            abstract = rv.get("abstract", "")
            if abstract:
                # 关联到具体划线的想法 → 随划线展示
                review_by_abstract.setdefault(abstract, []).append(rv)
            elif rv.get("chapterUid") is not None:
                # 有章节归属的评价 → 放在该章节
                ch_reviews[rv["chapterUid"]].append(rv)
            elif rv.get("chapterName"):
                # 从 chapterName 尝试匹配章节
                matched = False
                for cuid, ch in chapters_map.items():
                    if ch.get("title") == rv["chapterName"]:
                        ch_reviews[cuid].append(rv)
                        matched = True
                        break
                if not matched:
                    book_reviews.append(rv)
            else:
                # 无章节归属 → 整本书评价
                book_reviews.append(rv)

        all_cuids = set(ch_my_hl.keys()) | set(ch_reviews.keys())
        if social:
            for cuid, s in social.items():
                if s.get("highlights") or s.get("reviews"):
                    all_cuids.add(int(cuid) if isinstance(cuid, str) else cuid)

        # ── 保留用户填写的内容 ────────────────────────────────────────────
        user_intro = []
        user_thinking = []
        try:
            existing = self._n(self.client.blocks.children.list, block_id=page_id).get("results", [])
            section = None
            for blk in existing:
                bt = blk.get("type", "")
                if bt == "heading_2":
                    rich = blk[bt].get("rich_text", [])
                    text = rich[0]["plain_text"] if rich else ""
                    if "书籍简介" in text:
                        section = "intro"
                    elif "启迪思考" in text:
                        section = "thinking"
                    else:
                        section = None
                elif section == "intro":
                    user_intro.append(blk)
                elif section == "thinking":
                    user_thinking.append(blk)
        except Exception:
            pass

        blocks = []

        # ══════════════════════════════════════
        # 1. 书籍简介（黄色 h2，保留用户内容）
        # ══════════════════════════════════════
        blocks.append(_h2_colored("📖 书籍简介", "yellow"))
        if user_intro:
            blocks.extend(self._sanitize_block(b) for b in user_intro)
        else:
            intro = (book_info or {}).get("intro", "") or (book_info or {}).get("description", "")
            blocks.append(_paragraph(intro or "（暂无书籍简介，可在此处自行填写）"))
        blocks.append(_divider())

        # ══════════════════════════════════════
        # 2. 读书笔记（黄色 h2）
        # ══════════════════════════════════════
        blocks.append(_h2_colored("📝 读书笔记", "yellow"))
        blocks.append(_paragraph(f"最后同步：{datetime.now().strftime('%Y-%m-%d %H:%M')}"))

        def _sort_key(cuid):
            ch = chapters_map.get(cuid) or {}
            return ch.get("chapterIdx", 9999)

        for cuid in sorted(all_cuids, key=_sort_key):
            has_hl = bool(ch_my_hl.get(cuid))
            has_rv = bool(ch_reviews.get(cuid))
            if not has_hl and not has_rv:
                continue

            ch_info = chapters_map.get(cuid, {})
            ch_title = ch_info.get("title", f"章节 {cuid}")

            # 章节标题（绿色 h3，双重设色）
            ch_rich = [{
                "type": "text",
                "text": {"content": f"📑 {ch_title}"},
                "annotations": {"bold": False, "color": "green"},
            }]
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": ch_rich,
                                         "color": "green"}})

            # 划线
            for hl in ch_my_hl.get(cuid, []):
                mark_text = hl.get("markText", "")
                create_ts = hl.get("createTime", 0)
                date_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""
                blocks.append(_callout_green(f"「{mark_text}」"))
                for lrv in review_by_abstract.get(mark_text, []):
                    content = lrv.get("content", "")
                    if content:
                        blocks.append(_callout(f"💭 {content}", "💭"))
                if date_str:
                    blocks.append(_paragraph(f"  🕐 {date_str}"))

            # 章节评价
            for rv in ch_reviews.get(cuid, []):
                content = rv.get("content", "")
                star = rv.get("star", -1)
                create_ts = rv.get("createTime", 0)
                date_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""
                rating_str = f" ⭐{star}/5" if star and star > 0 else ""
                blocks.append(_callout(f"📝 章节评价{rating_str}\n{content}", "📝"))
                if date_str:
                    blocks.append(_paragraph(f"  🕐 {date_str}"))

        blocks.append(_divider())

        # ══════════════════════════════════════
        # 3. 书籍评价（黄色 h2）— 仅整本书评价
        # ══════════════════════════════════════
        blocks.append(_h2_colored("⭐ 书籍评价", "yellow"))
        if book_reviews:
            for rv in book_reviews:
                content = rv.get("content", "")
                star = rv.get("star", -1)
                create_ts = rv.get("createTime", 0)
                date_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""
                rating_str = f" ⭐{star}/5" if star and star > 0 else ""
                blocks.append(_callout(f"整本书评{rating_str}\n{content}", "📝"))
                if date_str:
                    blocks.append(_paragraph(f"🕐 {date_str}"))
        else:
            blocks.append(_paragraph("（暂无评价）"))
        blocks.append(_divider())

        # ══════════════════════════════════════
        # 4. 启迪思考（黄色 h2，保留用户内容）
        # ══════════════════════════════════════
        blocks.append(_h2_colored("💭 启迪思考", "yellow"))
        if user_thinking:
            blocks.extend(self._sanitize_block(b) for b in user_thinking)
        else:
            blocks.append(_paragraph("（在此处记录你的思考和感悟）"))
        blocks.append(_divider())

        # ══════════════════════════════════════
        # 5. 阅读统计（黄色 h2）
        # ══════════════════════════════════════
        blocks.append(_h2_colored("📊 阅读统计", "yellow"))

        # ── 本书累计阅读时长（来自 getprogress）─────────
        book_total_sec = 0
        if progress_info and progress_info.get("book"):
            p = progress_info["book"]
            for f in ["readTime", "readingTime", "totalReadTime", "duration"]:
                v = p.get(f, 0)
                if v and v > 0:
                    book_total_sec = int(v)
                    break

        note_count = len(highlights) + len(reviews)

        # ── 阅读进度 ───────────────────────────────
        raw_progress = 0
        if progress_info and progress_info.get("book"):
            raw_progress = progress_info["book"].get("progress", 0)
        progress_pct = raw_progress if raw_progress >= 0 else 0

        # ── 统计卡片（2 列 × 2 行）─
        def _make_card(icon, label, value, color) -> dict:
            return {
                "object": "block",
                "type": "column",
                "column": {
                    "children": [{
                        "object": "block",
                        "type": "callout",
                        "callout": {
                            "rich_text": [
                                {"type": "text", "text": {"content": label},
                                 "annotations": {"bold": False}},
                                {"type": "text", "text": {"content": "\n"}},
                                {"type": "text", "text": {"content": value},
                                 "annotations": {"bold": True}},
                            ],
                            "icon": {"type": "emoji", "emoji": icon},
                            "color": color,
                        }
                    }]
                }
            }

        cards = [
            ("⏱", "累计阅读", seconds_to_hm(book_total_sec) if book_total_sec > 0 else "-",
             "green_background"),
            ("📅", "阅读进度", f"{progress_pct}%" if progress_pct > 0 else "-",
             "blue_background"),
            ("📝", "笔记划线", f"{note_count} 条" if note_count > 0 else "-",
             "orange_background"),
            ("🏆", "开始日期", start_date or "-",
             "purple_background"),
        ]

        # 第 1 行：前 2 张
        blocks.append({
            "object": "block",
            "type": "column_list",
            "column_list": {"children": [_make_card(*cards[0]), _make_card(*cards[1])]},
        })
        # 第 2 行：后 2 张
        blocks.append({
            "object": "block",
            "type": "column_list",
            "column_list": {"children": [_make_card(*cards[2]), _make_card(*cards[3])]},
        })

        # ══════════════════════════════════════
        # 写入 Notion（安全策略：先清除旧内容，再写入新 blocks）
        # ══════════════════════════════════════
        # blocks 已完整构建，确保至少有 5 个 section 标题块才写入。
        # 如果 blocks 意外为空，跳过写入，保护既有内容不被清空。
        if not blocks or len(blocks) < 5:
            logger.warning(f"[{book_id}] 生成的 blocks 不足 5 个 (实际 {len(blocks)})，跳过写入，保护既有内容")
            return

        self._clear_page_content(page_id)
        self._append_blocks_chunked(page_id, blocks)

    # ── 页面操作辅助 ──────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_block(block: dict) -> dict:
        """
        从 Notion API 返回的原始 block 中提取有效字段。
        只保留 object、type、以及 type 对应的已知有效子字段，
        移除 id / has_children / parent / created_time / archived 等元数据。
        """
        bt = block.get("type", "paragraph")
        content = block.get(bt, {})
        # 仅保留类型特定的有效子字段，移除意外混入的 icon 等
        allowed_keys = {
            "paragraph": {"rich_text", "color", "children"},
            "heading_1": {"rich_text", "color", "is_toggleable"},
            "heading_2": {"rich_text", "color", "is_toggleable"},
            "heading_3": {"rich_text", "color", "is_toggleable"},
            "callout": {"rich_text", "icon", "color", "children"},
            "divider": set(),
            "column_list": {"children"},
            "column": {"children"},
            "bulleted_list_item": {"rich_text", "color", "children"},
            "numbered_list_item": {"rich_text", "color", "children"},
            "to_do": {"rich_text", "checked", "color", "children"},
            "toggle": {"rich_text", "color", "children"},
            "code": {"rich_text", "language", "caption"},
            "quote": {"rich_text", "color", "children"},
            "image": {"type", "external", "file", "caption"},
        }
        keep = allowed_keys.get(bt, set(content.keys()))
        cleaned = {k: v for k, v in content.items() if k in keep}
        return {"object": "block", "type": bt, bt: cleaned}

    def _clear_page_content(self, page_id: str):
        """删除页面内所有块（带重试）"""
        children = self._n(self.client.blocks.children.list, block_id=page_id)
        for block in children.get("results", []):
            def _del(bid=block["id"]):
                self._n(self.client.blocks.delete, block_id=bid)
            retry_on_failure(_del, max_retries=2, base_delay=0.5)

    def _append_blocks_chunked(self, page_id: str, blocks: list[dict], chunk_size: int = 100):
        """分批追加块（Notion API 单次上限 100，带重试）"""
        for i in range(0, len(blocks), chunk_size):
            chunk = blocks[i:i + chunk_size]

            def _append(ch=chunk):
                self._n(self.client.blocks.children.append,
                    block_id=page_id,
                    children=ch,
                )
            retry_on_failure(_append, max_retries=2, base_delay=0.5)
