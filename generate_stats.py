#!/usr/bin/env python3
"""独立脚本：从微信读书 API 获取数据，为书架所有书籍生成阅读统计 HTML"""

import os
import sys
from datetime import datetime
from pathlib import Path

# 确保能找到 weread_notion 模块
sys.path.insert(0, str(Path(__file__).resolve().parent))

from weread_notion.weread_client import WeReadClient
from weread_notion.stats_generator import save_book_stats


def main():
    api_key = os.environ.get("WEREAD_API_KEY", "")
    if not api_key:
        print("❌ 请设置环境变量 WEREAD_API_KEY")
        sys.exit(1)

    client = WeReadClient(api_key, request_delay=0.3)

    # 1. 获取书架
    print("📚 获取书架数据...")
    shelf = client.get_shelf()
    books = shelf.get("books", [])
    print(f"  共 {len(books)} 本电子书")

    # 2. 获取笔记概览
    print("📝 获取笔记概览...")
    notebooks = client.get_notebooks()
    notebook_map = {nb["bookId"]: nb for nb in notebooks}
    print(f"  {len(notebooks)} 本书有笔记")

    # 3. 获取本月每日阅读数据（跨书籍汇总）
    print("📅 获取本月阅读数据...")
    daily_records = []
    try:
        monthly_data = client.get_read_stats(mode="monthly")
        read_times = monthly_data.get("readTimes", {})
        for ts, sec in read_times.items():
            dt = datetime.fromtimestamp(int(ts))
            daily_records.append({
                "date": dt.strftime("%Y-%m-%d"),
                "sec": int(sec),
            })
        daily_records.sort(key=lambda r: r["date"])
        print(f"  本月 {len(daily_records)} 天有阅读记录")
    except Exception as e:
        print(f"  ⚠ 获取每日数据失败: {e}")

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 4. 逐本书获取数据并生成统计页
    print(f"\n📊 生成阅读统计页面（共 {len(books)} 本）...")
    success = 0
    for i, book in enumerate(books):
        book_id = book["bookId"]
        title = book.get("title", book_id)
        author = book.get("author", "")

        try:
            # 获取进度
            progress_info = client.get_book_progress(book_id)
            progress_raw = 0
            total_read_sec = 0
            start_date = ""

            if progress_info and progress_info.get("book"):
                p = progress_info["book"]
                progress_raw = p.get("progress", 0)
                for fld in ["readTime", "readingTime", "totalReadTime", "duration"]:
                    rt = p.get(fld, 0)
                    if rt and rt > 0:
                        total_read_sec = int(rt)
                        break
                for fld in ["firstReadTime", "firstOpenTime", "createTime"]:
                    ft = p.get(fld, 0)
                    if ft and ft > 0:
                        start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                        break

            if not start_date:
                for fld in ["createTime", "addTime", "readUpdateTime"]:
                    ft = book.get(fld, 0)
                    if ft and ft > 0:
                        start_date = datetime.fromtimestamp(ft).strftime("%Y-%m-%d")
                        break

            nb = notebook_map.get(book_id, {})
            note_count = nb.get("noteCount", 0)
            review_count = nb.get("reviewCount", 0)

            result = save_book_stats(
                book_id=book_id,
                title=title,
                author=author,
                total_read_sec=total_read_sec,
                progress=progress_raw,
                note_count=note_count,
                review_count=review_count,
                start_date=start_date,
                daily_records=daily_records,
                generated_at=generated_at,
            )
            if result:
                success += 1
                if (i + 1) % 20 == 0:
                    print(f"  进度: {i + 1}/{len(books)} (已生成 {success})")

        except Exception as e:
            print(f"  ✗ {title[:20]}: {e}")

    print(f"\n✅ 完成！成功生成 {success}/{len(books)} 本阅读统计")
    print(f"   文件位置: stats/ 目录")


if __name__ == "__main__":
    main()
