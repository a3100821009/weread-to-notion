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
