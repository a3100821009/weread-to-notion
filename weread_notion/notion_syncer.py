"""
Notion 同步模块
负责在 Notion 中创建/更新数据库和页面
"""

import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from notion_client import Client

# 封面存放目录
COVERS_DIR = Path("covers")
# 仓库已公开，使用 GitHub raw URL
GITHUB_COVER_BASE = "https://raw.githubusercontent.com/a3100821009/weread-to-notion/main/covers"


# ── Notion 富文本块辅助 ──────────────────────────────────────────────────────

def _text(content: str, bold: bool = False, color: str = "default") -> dict:
    """生成 rich_text 对象"""
    annotations = {"bold": bold, "color": color}
    return {
        "type": "text",
        "text": {"content": content[:2000]},  # Notion 单段限 2000 字符
        "annotations": annotations,
    }


def _rich(content: str, bold: bool = False) -> list[dict]:
    """生成 rich_text 数组（自动分割超长文本）"""
    if not content:
        return [_text("")]
    chunks = [content[i:i+2000] for i in range(0, len(content), 2000)]
    return [_text(c, bold) for c in chunks]


def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": _rich(text, bold=True)}}


def _heading3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rich(text)}}


def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich(text)}}


def _quote(text: str) -> dict:
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": _rich(text)}}


def _callout(text: str, emoji: str = "💡") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": _rich(text), "icon": {"type": "emoji", "emoji": emoji}}}

def _callout_green(text: str) -> dict:
    """绿色 callout — 自己的划线"""
    return _callout(text, "✏️")

def _callout_blue(text: str) -> dict:
    """蓝色 callout — 热门划线"""
    return _callout(text, "🔥")

def _callout_discuss(text: str) -> dict:
    """讨论 callout — 章节讨论"""
    return _callout(text, "💬")

def _heading_colored(text: str, color: str) -> dict:
    """带颜色的 H3 标题"""
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": _rich(text), "color": color}}


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text)}}


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


def _image_block(url: str, caption: str = "") -> dict:
    """生成 Notion 图片块（外部 URL）"""
    block = {
        "object": "block",
        "type": "image",
        "image": {"type": "external", "external": {"url": url}},
    }
    if caption:
        block["image"]["caption"] = _rich(caption)
    return block


def _chart_url(config: dict, width: int = 600, height: int = 400) -> str:
    """通过 QuickChart.io 生成图表图片 URL"""
    # QuickChart 免费 API：https://quickchart.io/documentation/
    encoded = urllib.parse.quote(json.dumps(config, ensure_ascii=False))
    return f"https://quickchart.io/chart?c={encoded}&w={width}&h={height}"


# ── 图表生成器 ────────────────────────────────────────────────────────────────

def _build_hourly_chart(prefer_time: list[int], total_sec: int) -> str:
    """阅读时段分布柱状图（preferTime 从 6:00 开始，24 个元素）"""
    if not prefer_time or len(prefer_time) != 24:
        return ""

    hours = []
    minutes_data = []
    for idx, sec in enumerate(prefer_time):
        hour = (idx + 6) % 24
        hours.append(f"{hour:02d}:00")
        minutes_data.append(round(sec / 60, 1))

    config = {
        "type": "bar",
        "data": {
            "labels": hours,
            "datasets": [{
                "label": "阅读时长（分钟）",
                "data": minutes_data,
                "backgroundColor": "#4A90D9",
                "borderRadius": 4,
            }],
        },
        "options": {
            "plugins": {
                "title": {"display": True, "text": "阅读时段分布", "font": {"size": 16}},
                "legend": {"display": False},
            },
            "scales": {
                "y": {"title": {"display": True, "text": "分钟"}},
            },
        },
    }
    return _chart_url(config, width=680, height=380)


def _build_category_chart(prefer_category: list[dict]) -> str:
    """偏好分类饼图"""
    if not prefer_category:
        return ""

    cats = prefer_category[:7]  # 最多 7 类
    labels = [c.get("categoryTitle", "其他") for c in cats]
    data = [round(c.get("readingTime", 0) / 3600, 1) for c in cats]  # 转小时

    colors = ["#4A90D9", "#7B68EE", "#E8913A", "#50C878", "#E85D75", "#20B2AA", "#DAA520"]

    config = {
        "type": "doughnut",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "阅读时长（小时）",
                "data": data,
                "backgroundColor": colors[:len(labels)],
                "borderWidth": 2,
                "borderColor": "#fff",
            }],
        },
        "options": {
            "plugins": {
                "title": {"display": True, "text": "偏好分类（按阅读时长）", "font": {"size": 16}},
            },
        },
    }
    return _chart_url(config, width=500, height=400)


def _build_author_chart(prefer_author: list[dict]) -> str:
    """偏好作者横向柱状图"""
    if not prefer_author:
        return ""

    authors = prefer_author[:8]
    # 反转顺序（QuickChart 横向柱状图从上往下）
    authors = list(reversed(authors))
    labels = [a.get("name", "") for a in authors]
    counts = [a.get("count", 0) for a in authors]

    config = {
        "type": "bar",
        "data": {
            "labels": labels,
            "datasets": [{
                "label": "阅读本数",
                "data": counts,
                "backgroundColor": "#E8913A",
                "borderRadius": 4,
            }],
        },
        "options": {
            "indexAxis": "y",
            "plugins": {
                "title": {"display": True, "text": "偏好作者 TOP8", "font": {"size": 16}},
                "legend": {"display": False},
            },
            "scales": {
                "x": {"title": {"display": True, "text": "本数"}, "ticks": {"stepSize": 1}},
            },
        },
    }
    return _chart_url(config, width=600, height=380)


# ── NotionSyncer ─────────────────────────────────────────────────────────────

class NotionSyncer:
    """
    负责将微信阅读数据写入 Notion。

    Notion 结构设计：
    ┌─ 父页面（用户指定）
    │  ├─ 📚 微信阅读书架（数据库）── 每本书一条记录
    │  │     属性：书名, 作者, 分类, 进度, 阅读时长, 完成状态, 最近阅读, 笔记数, 评分
    │  │     子页面：划线 & 想法（按章节分组）
    │  └─ 📊 阅读统计（普通页面）── 总体统计、偏好分析
    """

    SHELF_DB_TITLE = "📚 微信阅读书架"
    STATS_PAGE_TITLE = "📊 阅读统计"

    def __init__(self, token: str, parent_page_id: str, book_pages: Optional[dict] = None):
        self.client = Client(auth=token)
        self.parent_page_id = parent_page_id
        self._shelf_db_id: Optional[str] = None
        self._stats_page_id: Optional[str] = None
        self._book_pages: dict = book_pages or {}  # bookId -> notionPageId

    # ── 初始化结构 ──────────────────────────────────────────────────────────

    def setup(self):
        """初始化 Notion 结构（幂等，已存在则跳过）"""
        self._shelf_db_id = self._get_or_create_shelf_db()
        # 确保数据库包含所有最新字段（兼容已有数据库）
        self._ensure_shelf_db_properties()

    def _ensure_shelf_db_properties(self):
        """补充书籍数据库中可能缺失的新字段"""
        try:
            self.client.databases.update(
                database_id=self._shelf_db_id,
                properties={
                    "阅读时长": {"rich_text": {}},
                    "开始日期": {"date": {}},
                },
            )
        except Exception:
            pass

    def _search_in_parent(self, title: str, obj_type: str) -> Optional[str]:
        """在父页面中查找已存在的子页面/数据库"""
        result = self.client.search(query=title)
        for item in result.get("results", []):
            # 匹配对象类型
            if item.get("object") != obj_type:
                continue
            # 检查父级
            parent = item.get("parent", {})
            if parent.get("page_id", "").replace("-", "") == self.parent_page_id.replace("-", ""):
                return item["id"]
        return None

    def _get_or_create_shelf_db(self) -> str:
        """获取或创建书架数据库（作为父页面的子数据库）"""
        # 先搜索已有数据库
        db_id = self._search_in_parent(self.SHELF_DB_TITLE, "database")
        if db_id:
            return db_id

        db = self.client.databases.create(
            parent={"type": "page_id", "page_id": self.parent_page_id},
            title=[{"type": "text", "text": {"content": self.SHELF_DB_TITLE}}],
            icon={"type": "emoji", "emoji": "📚"},
            properties={
                "书名": {"title": {}},
                "作者": {"rich_text": {}},
                "分类": {"select": {}},
                "阅读进度": {
                    "number": {"format": "percent"}
                },
                "完成状态": {
                    "select": {
                        "options": [
                            {"name": "✅ 已读完", "color": "green"},
                            {"name": "📖 阅读中", "color": "blue"},
                            {"name": "📥 未开始", "color": "gray"},
                        ]
                    }
                },
                "评分": {
                    "number": {"format": "number"}
                },
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

    def _get_or_create_stats_page(self) -> str:
        """获取或创建阅读统计页面"""
        page_id = self._search_in_parent(self.STATS_PAGE_TITLE, "page")
        if page_id:
            return page_id

        page = self.client.pages.create(
            parent={"type": "page_id", "page_id": self.parent_page_id},
            properties={
                "title": {"title": [{"type": "text", "text": {"content": self.STATS_PAGE_TITLE}}]}
            },
            icon={"type": "emoji", "emoji": "📊"},
        )
        return page["id"]

    # ── 书架同步 ────────────────────────────────────────────────────────────

    def _find_book_page(self, book_id: str) -> Optional[str]:
        """在书架数据库中按 bookId 查找已有记录（先查本地缓存，再验证 Notion）"""
        notion_id = self._book_pages.get(book_id)
        if notion_id:
            try:
                self.client.pages.retrieve(page_id=notion_id)
                return notion_id
            except Exception:
                del self._book_pages[book_id]
        return None

    def sync_book(
        self,
        book_info: dict,
        progress_info: Optional[dict] = None,
        notebook_info: Optional[dict] = None,
    ) -> str:
        """
        同步单本书到书架数据库（创建或更新）
        返回 Notion 页面 ID
        """
        book_id = book_info.get("bookId", "")
        title = book_info.get("title", "未知书名")
        author = book_info.get("author", "")
        cover_url = book_info.get("cover", "")
        category = book_info.get("category", "")
        publisher = book_info.get("publisher", "")
        isbn = book_info.get("isbn", "")
        rating = book_info.get("newRating")  # 百分制，转 10 分制

        # 进度信息
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

            # 阅读时长（从进度数据中提取，单位秒 → 小时，保留 1 位小数）
            for field in ["readTime", "readingTime", "totalReadTime", "duration"]:
                rt = p.get(field, 0)
                if rt and rt > 0:
                    reading_hours = round(rt / 3600, 1)
                    break

            # 开始阅读日期
            for field in ["firstReadTime", "firstOpenTime", "createTime"]:
                ft = p.get(field, 0)
                if ft and ft > 0:
                    start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                    break

        # 笔记统计
        highlight_count = 0
        review_count = 0
        if notebook_info:
            highlight_count = notebook_info.get("noteCount", 0)
            review_count = notebook_info.get("reviewCount", 0)

        # 构建属性
        weread_url = f"weread://reading?bId={book_id}"
        properties: dict = {
            "书名": {"title": [{"type": "text", "text": {"content": title}}]},
            "作者": {"rich_text": _rich(author)},
            "分类": {"select": {"name": category or "其他"}} if category else {"select": {"name": "其他"}},
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
            # 更新已有页面：不覆盖书名（保留用户在 Notion 手动修改的标题）
            update_props = {k: v for k, v in properties.items() if k != "书名"}
            self.client.pages.update(
                page_id=existing_id,
                properties=update_props,
            )
            return existing_id
        else:
            page = self.client.pages.create(
                parent={"database_id": self._shelf_db_id},
                properties=properties,
                icon={"type": "emoji", "emoji": "📖"},
            )
            notion_id = page["id"]
            self._book_pages[book_id] = notion_id  # 记录映射
            return notion_id

    def delete_book_page(self, notion_page_id: str, book_id: str = "") -> bool:
        """
        从 Notion 中删除（归档）一本书的页面
        同时从本地缓存中移除映射
        """
        try:
            # 先清空页面内容（避免孤儿块）
            self._clear_page_content(notion_page_id)
            # 归档页面（Notion 的软删除）
            self.client.pages.update(
                page_id=notion_page_id,
                archived=True,
            )
            if book_id and book_id in self._book_pages:
                del self._book_pages[book_id]
            return True
        except Exception:
            # 页面可能已被手动删除，清理本地缓存即可
            if book_id and book_id in self._book_pages:
                del self._book_pages[book_id]
            return False

    def update_book_cover(self, notion_page_id: str, github_cover_url: str) -> bool:
        """更新单本书的页面封面 + 数据库"封面" files 属性"""
        if not github_cover_url:
            return False
        try:
            self.client.pages.update(
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
            return True
        except Exception:
            return False

    def batch_sync_covers(self, book_covers: dict[str, str], max_workers: int = 10) -> int:
        """
        并发批量下载封面到 covers/ 目录。
        book_covers: {book_id: weread_cover_url}
        max_workers: 并发数（默认 10，避免 CDN 限流）
        返回下载成功数
        """
        downloaded = 0

        def _download(bid: str, url: str) -> bool:
            return bool(_persist_cover(bid, url))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_download, bid, url): bid
                for bid, url in book_covers.items()
            }
            for future in as_completed(futures):
                if future.result():
                    downloaded += 1

        return downloaded

    def update_page_covers(self, max_workers: int = 10) -> int:
        """
        并发更新 Notion 页面封面（读取 covers/ 本地文件，构建 GitHub raw URL）。
        返回更新成功数。
        """
        updated = 0
        tasks: list[tuple[str, str]] = []

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
            futures = {
                pool.submit(_update, pid, url): bid
                for bid, pid, url in tasks
            }
            for future in as_completed(futures):
                if future.result():
                    updated += 1

        return updated

    # ── 笔记同步（划线 + 想法写入书籍页面内容） ─────────────────────────────

    def sync_book_notes(
        self,
        page_id: str,
        notes_data: dict,
        social_data: Optional[dict] = None,
        book_title: str = "",
    ):
        """
        将笔记写入书籍页面，每章四个模块：

        📝 章节摘要  — 字数 + 热门划线精要
        🟢 我的划线 & 想法  — 绿色 callout
        🔵 热门划线 & 想法  — 蓝色 callout
        💬 章节讨论  — 热门评论
        """
        highlights = notes_data.get("highlights", [])
        chapters_map = notes_data.get("chapters", {})
        reviews = notes_data.get("reviews", [])

        social = (social_data or {}).get("social", {})

        # 自己划线按章节分组
        from collections import defaultdict
        ch_my_hl: dict = defaultdict(list)
        for hl in highlights:
            ch_my_hl[hl.get("chapterUid", 0)].append(hl)

        # 自己想法按抽象关联
        review_by_abstract: dict = {}
        standalone_reviews = []
        for rv_item in reviews:
            rv = rv_item.get("review", {})
            abstract = rv.get("abstract", "")
            if abstract:
                review_by_abstract.setdefault(abstract, []).append(rv)
            else:
                standalone_reviews.append(rv)

        # 合并所有有内容的章节
        all_cuids = set(ch_my_hl.keys())
        for cuid, s in social.items():
            if s.get("highlights") or s.get("reviews"):
                all_cuids.add(int(cuid) if isinstance(cuid, str) else cuid)

        if not all_cuids and not standalone_reviews:
            return

        self._clear_page_content(page_id)

        blocks = []
        blocks.append(_heading2(f"{'《' + book_title + '》 ' if book_title else ''}阅读笔记"))
        blocks.append(_paragraph(f"最后同步：{datetime.now().strftime('%Y-%m-%d %H:%M')}"))
        blocks.append(_divider())

        def _sort_key(cuid):
            ch = chapters_map.get(cuid) or social.get(cuid) or {}
            return ch.get("chapterIdx", 9999)

        for cuid in sorted(all_cuids, key=_sort_key):
            ch_info = chapters_map.get(cuid, {})
            soc_info = social.get(cuid, {})
            ch_title = (
                ch_info.get("title")
                or soc_info.get("title")
                or f"章节 {cuid}"
            )
            word_count = ch_info.get("wordCount") or soc_info.get("wordCount", 0)

            # ═══════════════════════════════════
            # 章节标题
            # ═══════════════════════════════════
            blocks.append(_heading3(f"📑 {ch_title}"))

            # ═══════════════════════════════════
            # 模块一：章节摘要
            # ═══════════════════════════════════
            blocks.append(_heading_colored("📝 章节摘要", "gray"))

            summary_parts = []
            if word_count > 0:
                summary_parts.append(f"本章约 {word_count:,} 字")

            # 热门划线精要（取前 3 条作为摘要）
            soc_highlights = soc_info.get("highlights", [])
            if soc_highlights:
                top = sorted(soc_highlights, key=lambda h: h.get("totalCount", 0), reverse=True)[:3]
                summary_parts.append(f"共 {len(soc_highlights)} 条热门划线")

            if summary_parts:
                blocks.append(_paragraph(" · ".join(summary_parts)))

            if soc_highlights:
                top3 = sorted(soc_highlights, key=lambda h: h.get("totalCount", 0), reverse=True)[:3]
                blocks.append(_callout(
                    "本章精要：\n\n"
                    + "\n".join(
                        f"▸ 「{h.get('markText', '')[:120]}{'...' if len(h.get('markText', '')) > 120 else ''}」"
                        f"  — {h.get('totalCount', 0)}人划线"
                        for h in top3
                    ),
                    "📌"
                ))

            blocks.append(_divider())

            # ═══════════════════════════════════
            # 模块二：我的划线 & 想法
            # ═══════════════════════════════════
            my_highlights = ch_my_hl.get(cuid, [])
            blocks.append(_heading_colored(f"🟢 我的划线 & 想法（{len(my_highlights)} 条）", "green"))

            if my_highlights:
                for hl in my_highlights:
                    mark_text = hl.get("markText", "")
                    create_ts = hl.get("createTime", 0)
                    date_str = (
                        datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
                        if create_ts else ""
                    )
                    blocks.append(_callout_green(f"「{mark_text}」"))

                    # 关联我的想法
                    for lrv in review_by_abstract.get(mark_text, []):
                        content = lrv.get("content", "")
                        if content:
                            blocks.append(_callout(f"💭 {content}", "💭"))

                    if date_str:
                        blocks.append(_paragraph(f"  🕐 {date_str}"))
            else:
                blocks.append(_paragraph("（暂无划线）"))

            blocks.append(_divider())

            # ═══════════════════════════════════
            # 模块三：热门划线 & 想法
            # ═══════════════════════════════════
            blocks.append(_heading_colored(f"🔵 热门划线 & 想法（{len(soc_highlights)} 条）", "blue"))

            if soc_highlights:
                # 构建 range → reviews 映射
                soc_reviews = soc_info.get("reviews", [])
                reviews_by_range: dict = defaultdict(list)
                for rv in soc_reviews:
                    rng = rv.get("range", "")
                    reviews_by_range[rng].append(rv.get("review", {}))

                for sh in soc_highlights:
                    mark_text = sh.get("markText", "")
                    total_count = sh.get("totalCount", 0)

                    blocks.append(_callout_blue(
                        f"「{mark_text}」\n\n📊 {total_count} 人划线"
                    ))

                    # 关联热门想法
                    rng = sh.get("range", "")
                    linked_discuss = reviews_by_range.get(rng, [])
                    for lrv in linked_discuss[:3]:  # 每条划线最多显示 3 条热门想法
                        content = lrv.get("content", "")
                        author = lrv.get("author", {}) or {}
                        author_name = author.get("name", "匿名读者")
                        if content:
                            blocks.append(_callout_discuss(
                                f"{author_name}：{content[:300]}"
                            ))
            else:
                blocks.append(_paragraph("（暂无热门划线）"))

            blocks.append(_divider())

            # ═══════════════════════════════════
            # 模块四：章节讨论
            # ═══════════════════════════════════
            soc_reviews = soc_info.get("reviews", [])
            blocks.append(_heading_colored(f"💬 章节讨论（{len(soc_reviews)} 条）", "brown"))

            if soc_reviews:
                shown = 0
                for rv in soc_reviews:
                    review_obj = rv.get("review", {})
                    content = review_obj.get("content", "")
                    author = review_obj.get("author", {}) or {}
                    author_name = author.get("name", "匿名读者")
                    create_ts = review_obj.get("createTime", 0)
                    date_str = (
                        datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
                        if create_ts else ""
                    )
                    if content and len(content.strip()) > 3:
                        blocks.append(_callout_discuss(
                            f"**{author_name}**{' · ' + date_str if date_str else ''}\n{content[:400]}"
                        ))
                        shown += 1
                        if shown >= 5:
                            break
                if shown == 0:
                    blocks.append(_paragraph("（暂无优质讨论）"))
            else:
                blocks.append(_paragraph("（暂无章节讨论）"))

            blocks.append(_divider())

        # ═══════════════════════════════════
        # 书末：整本书评
        # ═══════════════════════════════════
        if standalone_reviews:
            blocks.append(_heading2("📝 书评"))
            for rv in standalone_reviews:
                content = rv.get("content", "")
                chapter_name = rv.get("chapterName", "")
                star = rv.get("star", -1)
                create_ts = rv.get("createTime", 0)
                date_str = (
                    datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d")
                    if create_ts else ""
                )
                label = f"【{chapter_name}】" if chapter_name else "【整本书评】"
                rating_str = f" ⭐{star}/5" if star and star > 0 else ""
                blocks.append(_callout(f"{label}{rating_str}\n{content}", "📝"))
                if date_str:
                    blocks.append(_paragraph(f"🕐 {date_str}"))
            blocks.append(_divider())

        self._append_blocks_chunked(page_id, blocks)

    def _clear_page_content(self, page_id: str):
        """删除页面内所有块"""
        children = self.client.blocks.children.list(block_id=page_id)
        for block in children.get("results", []):
            self.client.blocks.delete(block_id=block["id"])

    def _append_blocks_chunked(self, page_id: str, blocks: list[dict], chunk_size: int = 100):
        """分批追加块（Notion API 单次上限 100）"""
        for i in range(0, len(blocks), chunk_size):
            self.client.blocks.children.append(
                block_id=page_id,
                children=blocks[i:i + chunk_size],
            )

    # ── 阅读统计同步 ────────────────────────────────────────────────────────

    def sync_stats(self, stats: dict, mode_label: str = "总计"):
        """将阅读统计数据写入统计页面（含图表可视化）"""
        from weread_notion.weread_client import WeReadClient as WRC

        page_id = self._stats_page_id
        self._clear_page_content(page_id)

        total_sec = stats.get("totalReadTime", 0)
        read_days = stats.get("readDays", 0)
        total_hm = WRC.seconds_to_hm(total_sec)

        read_stat = stats.get("readStat", [])
        stat_map = {s["stat"]: s["counts"] for s in read_stat}

        blocks = [
            _heading2(f"📊 阅读统计 · {mode_label}"),
            _paragraph(f"最后同步：{datetime.now().strftime('%Y-%m-%d %H:%M')}"),
            _divider(),

            _heading3("⏱ 阅读时长"),
            _bullet(f"总阅读时长：{total_hm}"),
            _bullet(f"有效阅读天数：{read_days} 天"),
        ]

        # 读书统计
        if stat_map:
            blocks.append(_heading3("📈 阅读概况"))
            for k, v in stat_map.items():
                blocks.append(_bullet(f"{k}：{v}"))

        # ── 图表：阅读时段分布 ──
        prefer_time_arr = stats.get("preferTime", [])
        prefer_time_word = stats.get("preferTimeWord", "")
        if prefer_time_arr:
            blocks.append(_divider())
            blocks.append(_heading3("🕐 阅读时段"))

            # 生成时段分布图表
            hourly_url = _build_hourly_chart(prefer_time_arr, total_sec)
            if hourly_url:
                blocks.append(_image_block(hourly_url, "阅读时段分布"))

            if prefer_time_word:
                blocks.append(_callout(prefer_time_word, "🌙"))

            hours_detail = []
            for idx, sec in enumerate(prefer_time_arr):
                hour = (idx + 6) % 24
                if sec > 0:
                    hours_detail.append(f"{hour:02d}:00 — {WRC.seconds_to_hm(sec)}")
            if hours_detail:
                blocks.append(_paragraph("  " + " | ".join(hours_detail[:5])))

        # ── 图表：偏好分类 ──
        prefer_cat = stats.get("preferCategory", [])
        if prefer_cat:
            blocks.append(_divider())
            blocks.append(_heading3("📂 偏好分类"))

            cat_url = _build_category_chart(prefer_cat)
            if cat_url:
                blocks.append(_image_block(cat_url, "偏好分类（按阅读时长）"))

            for cat in prefer_cat[:8]:
                cat_title = cat.get("categoryTitle", "")
                reading_time = WRC.seconds_to_hm(cat.get("readingTime", 0))
                count = cat.get("readingCount", 0)
                blocks.append(_bullet(f"{cat_title}：{reading_time}（{count} 本）"))

        # ── 图表：偏好作者 ──
        prefer_authors = stats.get("preferAuthor", [])
        if prefer_authors:
            blocks.append(_divider())
            blocks.append(_heading3("✍️ 偏好作者"))

            author_url = _build_author_chart(prefer_authors)
            if author_url:
                blocks.append(_image_block(author_url, "偏好作者 TOP8"))

            for au in prefer_authors[:5]:
                name = au.get("name", "")
                count = au.get("count", 0)
                read_time = au.get("readTime", "")
                blocks.append(_bullet(f"{name}（{count} 本）{'— ' + read_time if read_time else ''}"))

        # 最多阅读书籍
        read_longest = stats.get("readLongest", [])
        if read_longest:
            blocks.append(_divider())
            blocks.append(_heading3("🏆 阅读最多"))
            for item in read_longest:
                bk = item.get("book") or item.get("albumInfo") or {}
                name = bk.get("title") or bk.get("name", "")
                rt = WRC.seconds_to_hm(item.get("readTime", 0))
                tags = item.get("tags", [])
                tag_str = " ".join(f"[{t}]" for t in tags) if tags else ""
                blocks.append(_bullet(f"{name}：{rt} {tag_str}".strip()))

        blocks.append(_divider())
        self._append_blocks_chunked(page_id, blocks)
