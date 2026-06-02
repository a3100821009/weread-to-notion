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

    cats = prefer_category[:7]
    labels = [c.get("categoryTitle", "其他") for c in cats]
    data = [round(c.get("readingTime", 0) / 3600, 1) for c in cats]

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


def _build_reading_stat_chart(
    total_sec: int,
    read_days: int,
    note_count: int,
    max_day_sec: int,
    max_day_label: str,
    start_label: str,
    read_records: list,
) -> str:
    """
    生成仿微信读书 App 阅读统计风格的图表。
    上方：4 个统计卡片（累计时长、阅读天数、笔记数、单日最久）
    下方：每日阅读水平条形图（日期 + 蓝色进度条 + 时长）
    """

    def sec_to_hm(sec: int) -> str:
        if sec <= 0:
            return "0分钟"
        h = sec // 3600
        m = (sec % 3600) // 60
        if h > 0 and m > 0:
            return f"{h}小时{m}分钟"
        elif h > 0:
            return f"{h}小时"
        else:
            return f"{m}分钟"

    # 每日明细
    day_data = []
    if read_records:
        for rec in read_records:
            d = rec.get("date", "") or rec.get("day", "")
            dur = rec.get("readTime", 0) or rec.get("duration", 0) or rec.get("readDuration", 0)
            if d and dur and dur > 0:
                label = d[-5:] if len(d) >= 5 else d  # MM-DD
                day_data.append((label, dur))
        day_data = sorted(day_data, key=lambda x: x[0])

    # 如果没有任何数据，返回空
    if total_sec == 0 and read_days == 0 and not day_data:
        return ""

    has_chart = len(day_data) >= 1

    # ── 用 QuickChart 生成组合图 ──────────────────────────────
    # 策略：上方用一个水平 bar 图展示汇总（4项统计用不同颜色），
    #        下方用水平 bar 展示每日记录
    # 为了实现"卡片样式"的统计，用 annotation + 自定义 HTML 不可行
    # 改用两段式：先生成统计卡片图，再生成每日进度图

    # 每日图（若有数据）
    if has_chart:
        labels_chart = [x[0] for x in day_data]
        data_chart = [round(x[1] / 60, 1) for x in day_data]
        max_val = max(data_chart) if data_chart else 1
        max_val_padded = max_val * 1.25  # 留右边空间显示标签

        # 构建带标签的水平条形图（仿App每日进度条）
        bar_config = {
            "type": "bar",
            "data": {
                "labels": labels_chart,
                "datasets": [{
                    "data": data_chart,
                    "backgroundColor": "#5BB8F5",
                    "borderRadius": 6,
                    "barThickness": 22,
                }],
            },
            "options": {
                "indexAxis": "y",
                "layout": {"padding": {"left": 10, "right": 80, "top": 10, "bottom": 10}},
                "plugins": {
                    "legend": {"display": False},
                    "datalabels": {
                        "anchor": "end",
                        "align": "right",
                        "formatter": "function(v){var h=Math.floor(v/60),m=Math.round(v%60);return h>0?(m>0?h+'小时'+m+'分':h+'小时'):m+'分钟';}",
                        "font": {"size": 11},
                        "color": "#444",
                    },
                },
                "scales": {
                    "x": {
                        "display": False,
                        "max": max_val_padded,
                        "grid": {"display": False},
                        "border": {"display": False},
                    },
                    "y": {
                        "grid": {"display": False},
                        "ticks": {
                            "font": {"size": 13, "weight": "bold"},
                            "color": "#5BB8F5",
                            "padding": 8,
                        },
                        "border": {"display": False},
                    },
                },
            },
        }

        # 动态高度：每行约 36px + 头部 60px
        chart_height = max(200, len(labels_chart) * 38 + 60)
        chart_url = _chart_url(bar_config, width=600, height=chart_height)
    else:
        chart_url = ""

    # 只返回每日进度条图（摘要统计已在外层以文字形式呈现）
    return chart_url


# ── Notion 的 heading_2 着色辅助 ─────────────────────────────────────────

def _h2_colored(text: str, color: str) -> dict:
    """带颜色的 H2 标题"""
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": _rich(text, bold=True), "color": color},
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

    def __init__(self, token: str, parent_page_id: str, book_pages: Optional[dict] = None):
        self.client = Client(auth=token)
        self.parent_page_id = parent_page_id
        self._shelf_db_id: Optional[str] = None
        self._book_pages: dict = book_pages or {}

    # ── 初始化结构 ──────────────────────────────────────────────────────────

    def setup(self):
        """初始化 Notion 结构（幂等，已存在则跳过）"""
        self._shelf_db_id = self._get_or_create_shelf_db()
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
        """在父页面中查找已存在的子页面/数据库（跳过已归档的）"""
        result = self.client.search(query=title)
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

        db = self.client.databases.create(
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
                self.client.pages.retrieve(page_id=notion_id)
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
            self.client.pages.update(page_id=existing_id, properties=update_props)
            return existing_id
        else:
            page = self.client.pages.create(
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
            self.client.pages.update(page_id=notion_page_id, archived=True)
            if book_id and book_id in self._book_pages:
                del self._book_pages[book_id]
            return True
        except Exception:
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
        """并发批量下载封面到 covers/ 目录"""
        downloaded = 0

        def _download(bid: str, url: str) -> bool:
            return bool(_persist_cover(bid, url))

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_download, bid, url): bid for bid, url in book_covers.items()}
            for future in as_completed(futures):
                if future.result():
                    downloaded += 1
        return downloaded

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
                        book_info=None, book_read_detail=None, progress_info=None):
        """
        重写书籍子页面，5 个 h2 模块（黄色背景），章节标题 h3（绿色背景）。
        保留用户在 书籍简介 和 启迪思考 中自行填写的内容。
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
        for cuid, s in social.items():
            if s.get("highlights") or s.get("reviews"):
                all_cuids.add(int(cuid) if isinstance(cuid, str) else cuid)

        if not all_cuids and not book_reviews:
            return

        # ── 保留用户填写的内容 ────────────────────────────────────────────
        user_intro = []
        user_thinking = []
        try:
            existing = self.client.blocks.children.list(block_id=page_id).get("results", [])
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

        # ── 清空页面 ────────────────────────────────────────────────────
        self._clear_page_content(page_id)

        blocks = []

        # ══════════════════════════════════════
        # 1. 书籍简介（黄色 h2，保留用户内容）
        # ══════════════════════════════════════
        blocks.append(_h2_colored("📖 书籍简介", "yellow"))
        if user_intro:
            blocks.extend(user_intro)
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
            ch_info = chapters_map.get(cuid, {})
            ch_title = ch_info.get("title", f"章节 {cuid}")

            # 章节标题（绿色背景 h3）
            blocks.append({"object": "block", "type": "heading_3",
                           "heading_3": {"rich_text": [{"type": "text", "text": {"content": f"📑 {ch_title}"}}],
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

            if not ch_my_hl.get(cuid) and not ch_reviews.get(cuid):
                blocks.append(_paragraph("（本章暂无划线）"))

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
            blocks.extend(user_thinking)
        else:
            blocks.append(_paragraph("（在此处记录你的思考和感悟）"))
        blocks.append(_divider())

        # ══════════════════════════════════════
        # 5. 阅读统计（黄色 h2）— 仿微信读书App样式
        # ══════════════════════════════════════
        blocks.append(_h2_colored("📊 阅读统计", "yellow"))

        # ── 收集每日阅读记录 ────────────────────────────────
        read_records = []
        if book_read_detail:
            for key in ["readRecords", "records", "dailyRead", "readStat", "readingRecords", "dailyRecords"]:
                records = book_read_detail.get(key, [])
                if records and isinstance(records, list):
                    read_records = records
                    break

        # ── 收集汇总数据 ─────────────────────────────────────
        total_sec = 0
        read_days = 0
        note_count = (notebook_info.get("noteCount", 0) if notebook_info else 0) + \
                     (notebook_info.get("reviewCount", 0) if notebook_info else 0)
        max_day_sec = 0
        max_day_label = ""
        start_label = ""

        # 从 book_read_detail 取汇总
        if book_read_detail:
            for f in ["totalReadTime", "readingTime", "totalTime"]:
                v = book_read_detail.get(f, 0)
                if v and v > 0:
                    total_sec = v
                    break
            for f in ["readDays", "readingDays", "days"]:
                v = book_read_detail.get(f, 0)
                if v and v > 0:
                    read_days = v
                    break

        # 从每日记录补充 & 算最长单日
        if read_records:
            day_data = []
            for rec in read_records:
                d = rec.get("date", "") or rec.get("day", "")
                dur = rec.get("readTime", 0) or rec.get("duration", 0) or rec.get("readDuration", 0)
                if dur and dur > 0:
                    day_data.append((d, dur))
            if day_data:
                if total_sec == 0:
                    total_sec = sum(v for _, v in day_data)
                if read_days == 0:
                    read_days = len(day_data)
                best = max(day_data, key=lambda x: x[1])
                max_day_sec = best[1]
                max_day_label = best[0][-5:] if len(best[0]) >= 5 else best[0]
                # 开始日期
                sorted_days = sorted(day_data, key=lambda x: x[0])
                start_label = sorted_days[0][0][-5:] if sorted_days else ""

        # 从阅读进度兜底
        if total_sec == 0 and progress_info and progress_info.get("book"):
            p = progress_info["book"]
            for f in ["readTime", "readingTime", "totalReadTime", "duration"]:
                v = p.get(f, 0)
                if v and v > 0:
                    total_sec = v
                    break

        def _sec_to_hm(sec: int) -> str:
            """秒 → 'X小时Y分钟' 或 'X分钟'"""
            if sec <= 0:
                return "0分钟"
            h = sec // 3600
            m = (sec % 3600) // 60
            if h > 0 and m > 0:
                return f"{h}小时{m}分钟"
            elif h > 0:
                return f"{h}小时"
            else:
                return f"{m}分钟"

        # ── 文字版汇总（仿App 4格统计卡片）──────────────────────
        summary_lines = []
        if total_sec > 0:
            summary_lines.append(f"⏱ 累计时长    {_sec_to_hm(total_sec)}")
            if start_label:
                summary_lines[-1] += f"    （{start_label} 开始阅读）"
        if read_days > 0:
            summary_lines.append(f"📅 阅读天数    {read_days} 天")
        if note_count > 0:
            summary_lines.append(f"📝 笔记/划线    {note_count} 条")
        if max_day_sec > 0:
            max_label = f"（{max_day_label}）" if max_day_label else ""
            summary_lines.append(f"🏆 单日最久    {_sec_to_hm(max_day_sec)} {max_label}")

        for line in summary_lines:
            blocks.append(_paragraph(line))

        # ── 每日阅读进度条图 ─────────────────────────────────────
        stat_chart_url = _build_reading_stat_chart(
            total_sec=total_sec,
            read_days=read_days,
            note_count=note_count,
            max_day_sec=max_day_sec,
            max_day_label=max_day_label,
            start_label=start_label,
            read_records=read_records,
        )
        if stat_chart_url:
            blocks.append(_image_block(stat_chart_url, "每日阅读记录"))
        elif not summary_lines:
            blocks.append(_paragraph("（阅读统计数据暂不可用）"))

        # ══════════════════════════════════════
        # 写入 Notion
        # ══════════════════════════════════════
        self._append_blocks_chunked(page_id, blocks)

    # ── 页面操作辅助 ──────────────────────────────────────────────────────

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
