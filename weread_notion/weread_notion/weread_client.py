"""
微信阅读 API 客户端
封装所有与微信阅读 Agent Gateway 的通信逻辑
"""

import time
import os
from typing import Optional, Generator
import requests

GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
SKILL_VERSION = "1.0.3"


class WeReadClient:
    """微信阅读 API 客户端"""

    def __init__(self, api_key: str, request_delay: float = 0.3):
        self.api_key = api_key
        self.request_delay = request_delay
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        self._last_request_time = 0.0

    def _call(self, api_name: str, **params) -> dict:
        """统一 API 调用入口，自动加速限制、版本上报和升级检查"""
        # 限速
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)

        body = {"api_name": api_name, "skill_version": SKILL_VERSION, **params}
        resp = self.session.post(GATEWAY_URL, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._last_request_time = time.time()

        # 检查升级指令
        if "upgrade_info" in data:
            raise RuntimeError(
                f"[WeRead Skill 需要升级] {data['upgrade_info'].get('message', '')}"
            )

        if data.get("errcode", 0) != 0:
            raise RuntimeError(
                f"API 错误 [{api_name}]: errcode={data.get('errcode')}, "
                f"errmsg={data.get('errmsg', '')}"
            )

        return data

    # -------------------------------------------------------------------------
    # 书架
    # -------------------------------------------------------------------------

    def get_shelf(self) -> dict:
        """获取完整书架（电子书 + 专辑/有声书）"""
        return self._call("/shelf/sync")

    # -------------------------------------------------------------------------
    # 书籍信息
    # -------------------------------------------------------------------------

    def get_book_info(self, book_id: str) -> dict:
        """获取书籍基本信息"""
        return self._call("/book/info", bookId=book_id)

    def get_book_chapters(self, book_id: str) -> dict:
        """获取书籍章节目录"""
        return self._call("/book/chapterinfo", bookId=book_id)

    def get_book_progress(self, book_id: str) -> dict:
        """获取某本书的阅读进度"""
        return self._call("/book/getprogress", bookId=book_id)

    # -------------------------------------------------------------------------
    # 阅读统计
    # -------------------------------------------------------------------------

    def get_read_stats(self, mode: str = "overall", base_time: int = 0) -> dict:
        """
        获取阅读统计
        mode: weekly | monthly | annually | overall
        base_time: 基准时间戳，0 = 当前周期
        """
        params = {"mode": mode}
        if base_time:
            params["baseTime"] = base_time
        return self._call("/readdata/detail", **params)

    # -------------------------------------------------------------------------
    # 笔记 / 划线 / 想法
    # -------------------------------------------------------------------------

    def get_notebooks(self) -> list[dict]:
        """
        获取所有有笔记的书籍概览（自动翻页，返回完整列表）
        每条包含书籍信息、划线数、想法数、书签数等
        """
        results = []
        last_sort: Optional[int] = None

        while True:
            params: dict = {"count": 100}
            if last_sort is not None:
                params["lastSort"] = last_sort

            data = self._call("/user/notebooks", **params)
            books = data.get("books", [])
            results.extend(books)

            if not data.get("hasMore") or not books:
                break

            last_sort = books[-1]["sort"]

        return results

    def get_book_highlights(self, book_id: str) -> dict:
        """获取某本书的全部划线（含章节信息）"""
        return self._call("/book/bookmarklist", bookId=book_id)

    def get_book_reviews(self, book_id: str) -> list[dict]:
        """
        获取某本书的全部个人想法/点评（自动翻页）
        包含划线想法、章节点评、整本书评
        """
        results = []
        synckey = 0

        while True:
            data = self._call(
                "/review/list/mine",
                bookid=book_id,
                synckey=synckey,
                count=100,
            )
            reviews = data.get("reviews", [])
            results.extend(reviews)

            if not data.get("hasMore"):
                break

            synckey = data.get("synckey", 0)

        return results

    # -------------------------------------------------------------------------
    # 便捷：一次性拉取某本书的完整笔记（划线 + 想法合并）
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # 热门划线 & 社交笔记
    # -------------------------------------------------------------------------

    def get_best_bookmarks(self, book_id: str, chapter_uid: int = 0) -> dict:
        """获取热门划线（全书或单章），最多 20 条，含划线原文和人数"""
        return self._call("/book/bestbookmarks", bookId=book_id, chapterUid=chapter_uid)

    def get_read_reviews(
        self, book_id: str, chapter_uid: int, ranges: list[dict]
    ) -> dict:
        """获取指定划线下的热门评论/想法"""
        return self._call(
            "/book/readreviews",
            bookId=book_id,
            chapterUid=chapter_uid,
            reviews=ranges,
        )

    def get_book_social_notes(self, book_id: str) -> dict:
        """
        获取全书社交笔记数据，结构：
        {
            "chapters": {...},       # chapterUid -> {title, chapterIdx, wordCount}
            "social": {...},         # chapterUid -> {highlights: [...], reviews: [...]}
        }
        """
        # 获取章节目录（含字数）
        chapter_data = self.get_book_chapters(book_id)
        chapters_list = chapter_data.get("chapters", [])
        chapters_map: dict[int, dict] = {}
        for ch in chapters_list:
            chapters_map[ch["chapterUid"]] = {
                "title": ch.get("title", ""),
                "chapterIdx": ch.get("chapterIdx", 0),
                "wordCount": ch.get("wordCount", 0),
            }

        # 获取全书热门划线（按章节筛选）
        social: dict[int, dict] = {}

        try:
            all_best = self.get_best_bookmarks(book_id, chapter_uid=0)
            items = all_best.get("items", [])
            best_chapters = all_best.get("chapters", [])

            # 构建章节映射
            best_ch_map: dict[int, dict] = {}
            for ch in best_chapters:
                best_ch_map[ch["chapterUid"]] = ch

            # 按章节分组热门划线
            for item in items:
                cuid = item.get("chapterUid", 0)
                if cuid not in social:
                    social[cuid] = {"highlights": [], "reviews": []}
                social[cuid]["highlights"].append(item)

            # 对每个有热门划线的章节，获取划线下评论
            for cuid, data in social.items():
                ranges = []
                for hl in data["highlights"]:
                    rng = hl.get("range", "")
                    if rng:
                        ranges.append({"range": rng, "count": 5, "maxIdx": 0})
                if ranges:
                    try:
                        rv_data = self.get_read_reviews(book_id, cuid, ranges)
                        rv_list = rv_data.get("reviews", [])
                        for rv_entry in rv_list:
                            page_reviews = rv_entry.get("pageReviews", [])
                            for pr in page_reviews:
                                pr_range = rv_entry.get("range", "")
                                if pr_range not in data:
                                    data[pr_range] = []
                                if isinstance(data.get(pr_range), list):
                                    data["reviews"].append({
                                        "range": pr_range,
                                        "review": pr.get("review", {}),
                                    })
                    except Exception:
                        pass  # 某些书可能没有评论功能

        except Exception:
            pass  # 热门划线不是必需功能

        # 合并章节信息（包含没有热门划线的章节）
        for cuid, info in chapters_map.items():
            if cuid not in social:
                social[cuid] = {"highlights": [], "reviews": []}
            social[cuid].update(info)

        return {
            "chapters": chapters_map,
            "social": social,
        }

    def get_book_notes(self, book_id: str) -> dict:
        """
        返回某本书的完整笔记数据，结构：
        {
            "highlights": [...],   # 划线列表
            "chapters": {...},     # chapterUid -> {title, chapterIdx}
            "reviews": [...],      # 想法列表
        }
        """
        hl_data = self.get_book_highlights(book_id)
        reviews = self.get_book_reviews(book_id)

        # 构建 chapterUid -> chapter 映射
        chapters_map: dict[int, dict] = {}
        for ch in hl_data.get("chapters", []):
            chapters_map[ch["chapterUid"]] = ch

        return {
            "highlights": hl_data.get("updated", []),
            "chapters": chapters_map,
            "reviews": reviews,
        }

    # -------------------------------------------------------------------------
    # 工具方法
    # -------------------------------------------------------------------------

    @staticmethod
    def seconds_to_hm(seconds: int) -> str:
        """将秒数格式化为 'X小时Y分钟'"""
        if seconds <= 0:
            return "0分钟"
        h = seconds // 3600
        m = (seconds % 3600) // 60
        if h > 0 and m > 0:
            return f"{h}小时{m}分钟"
        elif h > 0:
            return f"{h}小时"
        else:
            return f"{m}分钟"

    @staticmethod
    def ts_to_date(ts: int) -> str:
        """Unix 时间戳 → YYYY-MM-DD"""
        if not ts:
            return ""
        import datetime
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
