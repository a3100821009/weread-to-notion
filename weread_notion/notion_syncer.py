"""
Notion 同步模块
负责在 Notion 中创建/更新数据库和页面
"""

import re
from datetime import datetime
from typing import Optional
from notion_client import Client


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


def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _rich(text)}}


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
        self._stats_page_id = self._get_or_create_stats_page()

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
        """获取或创建书架数据库"""
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

        # 封面
        cover_prop = None
        if cover_url:
            cover_prop = {"type": "external", "external": {"url": cover_url}}

        existing_id = self._find_book_page(book_id)
        if existing_id:
            self.client.pages.update(
                page_id=existing_id,
                properties=properties,
                cover=cover_prop,
            )
            return existing_id
        else:
            page = self.client.pages.create(
                parent={"database_id": self._shelf_db_id},
                properties=properties,
                cover=cover_prop,
                icon={"type": "emoji", "emoji": "📖"},
            )
            notion_id = page["id"]
            self._book_pages[book_id] = notion_id  # 记录映射
            return notion_id

    # ── 笔记同步（划线 + 想法写入书籍页面内容） ─────────────────────────────

    def sync_book_notes(self, page_id: str, notes_data: dict, book_title: str = ""):
        """
        将划线和想法写入书籍页面（清空旧内容后重写）
        notes_data 结构来自 WeReadClient.get_book_notes()
        """
        highlights = notes_data.get("highlights", [])
        chapters_map = notes_data.get("chapters", {})
        reviews = notes_data.get("reviews", [])

        if not highlights and not reviews:
            return

        # 先清除旧块
        self._clear_page_content(page_id)

        blocks = []

        # 标题
        blocks.append(_heading2(f"{'《' + book_title + '》 ' if book_title else ''}笔记"))
        blocks.append(_paragraph(f"最后同步：{datetime.now().strftime('%Y-%m-%d %H:%M')}"))
        blocks.append(_divider())

        # 按章节分组划线
        from collections import defaultdict
        chapter_highlights: dict = defaultdict(list)
        for hl in highlights:
            cuid = hl.get("chapterUid", 0)
            chapter_highlights[cuid].append(hl)

        # 按章节序号排序
        def chapter_sort_key(cuid):
            ch = chapters_map.get(cuid, {})
            return ch.get("chapterIdx", 9999)

        sorted_chapters = sorted(chapter_highlights.keys(), key=chapter_sort_key)

        # 构建 reviewId -> review 映射，用于关联划线想法
        review_by_abstract: dict = {}
        standalone_reviews = []
        for rv_item in reviews:
            rv = rv_item.get("review", {})
            abstract = rv.get("abstract", "")
            if abstract:
                review_by_abstract.setdefault(abstract, []).append(rv)
            else:
                standalone_reviews.append(rv)

        for cuid in sorted_chapters:
            ch_info = chapters_map.get(cuid, {})
            ch_title = ch_info.get("title", f"章节 {cuid}")
            blocks.append(_heading3(f"📑 {ch_title}"))

            for hl in chapter_highlights[cuid]:
                mark_text = hl.get("markText", "")
                create_ts = hl.get("createTime", 0)
                date_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""

                # 划线原文
                blocks.append(_quote(mark_text))

                # 关联想法
                linked_reviews = review_by_abstract.get(mark_text, [])
                for lrv in linked_reviews:
                    content = lrv.get("content", "")
                    if content:
                        blocks.append(_callout(f"💭 {content}", "💭"))

                if date_str:
                    blocks.append(_paragraph(f"  ↑ {date_str}"))

            blocks.append(_divider())

        # 整本书评/章节点评（无关联划线的想法）
        if standalone_reviews:
            blocks.append(_heading3("📝 书评与点评"))
            for rv in standalone_reviews:
                content = rv.get("content", "")
                chapter_name = rv.get("chapterName", "")
                star = rv.get("star", -1)
                create_ts = rv.get("createTime", 0)
                date_str = datetime.fromtimestamp(create_ts).strftime("%Y-%m-%d") if create_ts else ""

                label = f"【{chapter_name}】" if chapter_name else "【整本书评】"
                rating_str = f" ⭐{star}/5" if star and star > 0 else ""
                blocks.append(_callout(f"{label}{rating_str}\n{content}", "📝"))
                if date_str:
                    blocks.append(_paragraph(f"  ↑ {date_str}"))

            blocks.append(_divider())

        # Notion API 每次最多追加 100 个块
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
        """将阅读统计数据写入统计页面"""
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

        # 偏好分类
        prefer_cat = stats.get("preferCategory", [])
        if prefer_cat:
            blocks.append(_heading3("📂 偏好分类"))
            for cat in prefer_cat[:8]:
                cat_title = cat.get("categoryTitle", "")
                reading_time = WRC.seconds_to_hm(cat.get("readingTime", 0))
                count = cat.get("readingCount", 0)
                blocks.append(_bullet(f"{cat_title}：{reading_time}（{count} 本）"))

        # 偏好时段
        prefer_time_word = stats.get("preferTimeWord", "")
        prefer_time_arr = stats.get("preferTime", [])
        if prefer_time_word:
            blocks.append(_heading3("🕐 阅读时段"))
            blocks.append(_callout(prefer_time_word, "🌙"))
            if prefer_time_arr:
                # preferTime 从 6:00 开始
                hours = []
                for idx, sec in enumerate(prefer_time_arr):
                    hour = (idx + 6) % 24
                    if sec > 0:
                        hours.append(f"{hour:02d}:00 — {WRC.seconds_to_hm(sec)}")
                if hours:
                    for h in hours[:5]:  # 显示前 5 个高峰时段
                        blocks.append(_bullet(h))

        # 偏好作者
        prefer_authors = stats.get("preferAuthor", [])
        if prefer_authors:
            blocks.append(_heading3("✍️ 偏好作者"))
            for au in prefer_authors[:5]:
                name = au.get("name", "")
                count = au.get("count", 0)
                read_time = au.get("readTime", "")
                blocks.append(_bullet(f"{name}（{count} 本）{'— ' + read_time if read_time else ''}"))

        # 最多阅读书籍
        read_longest = stats.get("readLongest", [])
        if read_longest:
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
