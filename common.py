"""
共享常量和工具函数
集中管理跨模块复用的配置值和工具方法
"""

# ── 封面相关 ────────────────────────────────────────────────────────────────
# GitHub raw URL（仓库已公开）
GITHUB_COVER_BASE = "https://raw.githubusercontent.com/a3100821009/weread-to-notion/main/covers"


# ── 时间工具 ─────────────────────────────────────────────────────────────────

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




# ── 阅读时间提取 ─────────────────────────────────────────────────────────────

def extract_reading_time(book_shelf: dict, progress_info: dict) -> int:
    """
    从书架/进度数据中提取阅读时间（秒）。
    返回 0 表示无法确定；-1 表示有进度但无精确时间（已读但未知时长）。
    尝试多个可能的字段名以兼容不同 API 版本。
    """
    # 从书架数据获取
    for field in ["readTime", "readingTime", "totalReadTime", "readDuration"]:
        val = book_shelf.get(field, 0)
        if val and val > 0:
            return int(val)

    # 从进度数据获取
    book_prog = (progress_info or {}).get("book", {})
    for field in ["readTime", "readingTime", "totalReadTime", "duration"]:
        val = book_prog.get(field, 0)
        if val and val > 0:
            return int(val)

    # 兜底：有进度但无精确时间
    prog = book_prog.get("progress", 0)
    if prog > 0:
        return -1

    return 0


def extract_start_date(progress_info: dict, book_info: dict = None, book_shelf: dict = None) -> str:
    """
    从进度/书籍信息中提取首次阅读日期（YYYY-MM-DD）。
    依次尝试：进度数据 → 书籍信息 → 书架数据。
    """
    # 从进度数据
    if progress_info and progress_info.get("book"):
        p = progress_info["book"]
        for fld in ["firstReadTime", "firstOpenTime", "createTime"]:
            ft = p.get(fld, 0)
            if ft and ft > 0:
                return seconds_to_date(ft)

    # 从书籍信息
    if book_info:
        for fld in ["createTime", "addTime", "create_time", "add_time"]:
            ft = book_info.get(fld, 0)
            if ft and ft > 0:
                return seconds_to_date(ft)

    # 从书架数据
    if book_shelf:
        for fld in ["createTime", "addTime", "readUpdateTime"]:
            ft = book_shelf.get(fld, 0)
            if ft and ft > 0:
                return seconds_to_date(ft)

    return ""


def seconds_to_date(ts: int) -> str:
    """Unix 时间戳 → YYYY-MM-DD"""
    if not ts:
        return ""
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


# ── API 重试 ─────────────────────────────────────────────────────────────────

import time
import logging

logger = logging.getLogger(__name__)


def retry_on_failure(
    fn,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: tuple = (ConnectionError, TimeoutError, OSError),
    status_forcelist: tuple = (429, 500, 502, 503, 504),
):
    """
    指数退避重试装饰器（函数级）。

    用法：
        def my_call():
            ...
        result = retry_on_failure(my_call)

    或作为上下文：
        with retry_context(...):
            ...
    """
    import requests

    attempt = 0
    while True:
        try:
            return fn()
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status in status_forcelist:
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    attempt += 1
                    logger.warning(
                        f"HTTP {status}, 重试 {attempt}/{max_retries} "
                        f"(等待 {delay:.1f}s)..."
                    )
                    time.sleep(delay)
                    continue
            raise
        except retryable_exceptions:
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                attempt += 1
                logger.warning(
                    f"网络错误, 重试 {attempt}/{max_retries} "
                    f"(等待 {delay:.1f}s)..."
                )
                time.sleep(delay)
                continue
            raise
