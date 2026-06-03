#!/usr/bin/env python3
"""
weread-to-notion — 微信阅读同步到 Notion 的命令行工具
"""

import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

console = Console()

# 自动加载 .env 文件
load_dotenv(Path(__file__).parent / ".env")
load_dotenv()  # 也尝试当前目录


def _require_env(name: str, flag_val: str = "") -> str:
    val = flag_val or os.environ.get(name, "")
    if not val:
        console.print(f"[red]❌ 缺少配置：{name}[/red]")
        console.print(f"   请在 .env 文件或环境变量中设置 {name}")
        sys.exit(1)
    return val


@click.group()
def cli():
    """微信阅读 → Notion 同步工具\n\n将微信阅读的书架、笔记、统计数据同步到 Notion。"""
    pass


@cli.command("sync")
@click.option("--weread-key", envvar="WEREAD_API_KEY", help="微信阅读 API Key (wrk-...)")
@click.option("--notion-token", envvar="NOTION_TOKEN", help="Notion 集成 Token (secret_...)")
@click.option("--parent-page-id", envvar="NOTION_PARENT_PAGE_ID", help="Notion 父页面 ID")
@click.option("--no-highlights", is_flag=True, default=False, help="跳过划线同步")
@click.option("--no-reviews", is_flag=True, default=False, help="跳过想法同步")
@click.option("--full", is_flag=True, default=False, help="全量同步（忽略增量状态）")
@click.option("--delay", default=0.1, show_default=True, help="API 请求间隔（秒）")
def sync_cmd(
    weread_key, notion_token, parent_page_id,
    no_highlights, no_reviews, full, delay
):
    """执行同步：书架、划线、想法 → Notion"""
    weread_key = _require_env("WEREAD_API_KEY", weread_key)
    notion_token = _require_env("NOTION_TOKEN", notion_token)
    parent_page_id = _require_env("NOTION_PARENT_PAGE_ID", parent_page_id)

    from weread_notion.sync_manager import SyncManager

    mgr = SyncManager(
        weread_key=weread_key,
        notion_token=notion_token,
        parent_page_id=parent_page_id,
        sync_highlights=not no_highlights,
        sync_reviews=not no_reviews,
        request_delay=delay,
        incremental=not full,
    )
    mgr.run()


@cli.command("shelf")
@click.option("--weread-key", envvar="WEREAD_API_KEY")
def shelf_cmd(weread_key):
    """仅查看书架（不同步到 Notion）"""
    weread_key = _require_env("WEREAD_API_KEY", weread_key)

    from weread_notion.weread_client import WeReadClient
    from rich.table import Table

    wr = WeReadClient(weread_key)
    shelf = wr.get_shelf()
    books = shelf.get("books", [])
    albums = shelf.get("albums", [])

    table = Table(title=f"📚 微信读书书架（共 {len(books)} 本电子书，{len(albums)} 个专辑）")
    table.add_column("#", style="dim", width=4)
    table.add_column("书名", style="cyan")
    table.add_column("作者", style="green")
    table.add_column("分类")
    table.add_column("最近阅读", style="dim")

    for i, book in enumerate(books, 1):
        last_read = WeReadClient.ts_to_date(book.get("readUpdateTime", 0))
        table.add_row(
            str(i),
            book.get("title", ""),
            book.get("author", ""),
            book.get("category", ""),
            last_read,
        )

    console.print(table)


@cli.command("stats")
@click.option("--weread-key", envvar="WEREAD_API_KEY")
@click.option("--mode", default="overall",
              type=click.Choice(["weekly", "monthly", "annually", "overall"]),
              help="统计维度")
def stats_cmd(weread_key, mode):
    """仅查看阅读统计（不同步到 Notion）"""
    weread_key = _require_env("WEREAD_API_KEY", weread_key)

    from weread_notion.weread_client import WeReadClient
    from rich.table import Table

    wr = WeReadClient(weread_key)
    stats = wr.get_read_stats(mode=mode)

    total_hm = WeReadClient.seconds_to_hm(stats.get("totalReadTime", 0))
    read_days = stats.get("readDays", 0)

    console.print(f"\n📊 [bold]阅读统计 · {mode}[/bold]")
    console.print(f"  总阅读时长：[cyan]{total_hm}[/cyan]")
    console.print(f"  有效阅读天数：[cyan]{read_days} 天[/cyan]")

    for stat in stats.get("readStat", []):
        console.print(f"  {stat['stat']}：[cyan]{stat['counts']}[/cyan]")

    prefer_cat = stats.get("preferCategory", [])
    if prefer_cat:
        console.print("\n  [bold]偏好分类：[/bold]")
        for cat in prefer_cat[:5]:
            t = WeReadClient.seconds_to_hm(cat.get("readingTime", 0))
            console.print(f"    • {cat['categoryTitle']}（{t}）")


@cli.command("notes")
@click.argument("book_name")
@click.option("--weread-key", envvar="WEREAD_API_KEY")
def notes_cmd(book_name, weread_key):
    """查看某本书的划线与想法（按书名搜索，不同步 Notion）"""
    weread_key = _require_env("WEREAD_API_KEY", weread_key)

    from weread_notion.weread_client import WeReadClient
    from rich.table import Table

    wr = WeReadClient(weread_key)

    # 搜索书籍
    console.print(f"[yellow]搜索《{book_name}》...[/yellow]")
    search_data = wr._call("/store/search", keyword=book_name, count=5)
    books_found = search_data.get("books", [])

    if not books_found:
        console.print("[red]未找到该书[/red]")
        return

    book = books_found[0]
    book_id = book.get("bookId", "")
    title = book.get("title", "")
    console.print(f"[green]找到：《{title}》[/green]")

    notes = wr.get_book_notes(book_id)
    highlights = notes["highlights"]
    reviews_raw = notes["reviews"]
    chapters_map = notes["chapters"]

    console.print(f"\n  划线：[cyan]{len(highlights)}[/cyan] 条")
    console.print(f"  想法：[cyan]{len(reviews_raw)}[/cyan] 条\n")

    from collections import defaultdict
    chapter_hls = defaultdict(list)
    for hl in highlights:
        chapter_hls[hl.get("chapterUid", 0)].append(hl)

    for cuid, hls in chapter_hls.items():
        ch = chapters_map.get(cuid, {})
        console.print(f"[bold]── {ch.get('title', '章节 ' + str(cuid))}[/bold]")
        for hl in hls:
            console.print(f"  [cyan]❝[/cyan] {hl.get('markText', '')}")
        console.print()


if __name__ == "__main__":
    cli()
