"""
主同步流程：协调微信阅读和 Notion 两侧的数据同步
"""

import json
import os
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.panel import Panel
from rich.table import Table

from weread_notion.weread_client import WeReadClient
from weread_notion.notion_syncer import NotionSyncer

console = Console()

STATE_FILE = Path("sync_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class SyncManager:
    def __init__(
        self,
        weread_key: str,
        notion_token: str,
        parent_page_id: str,
        sync_highlights: bool = True,
        sync_reviews: bool = True,
        sync_stats: bool = True,
        request_delay: float = 0.3,
        incremental: bool = True,
    ):
        self.wr = WeReadClient(weread_key, request_delay)
        self.sync_highlights = sync_highlights
        self.sync_reviews = sync_reviews
        self.sync_stats = sync_stats
        self.incremental = incremental
        self.state = load_state()
        self.ns = NotionSyncer(
            notion_token, parent_page_id,
            book_pages=self.state.get("book_pages", {}),
        )

    def run(self):
        console.print(Panel.fit(
            "[bold cyan]微信阅读 → Notion 同步[/bold cyan]\n"
            "[dim]WeRead to Notion Sync Tool[/dim]",
            border_style="cyan"
        ))

        # 初始化 Notion 结构
        console.print("\n[yellow]▶ 初始化 Notion 结构...[/yellow]")
        self.ns.setup()
        console.print("[green]✓ Notion 结构就绪[/green]")

        # 1. 同步书架
        self._sync_shelf()

        # 2. 同步阅读统计
        if self.sync_stats:
            self._sync_stats()

        # 回存 book_pages 映射
        self.state["book_pages"] = self.ns._book_pages
        save_state(self.state)
        console.print("\n[bold green]✅ 同步完成！[/bold green]")

    def _sync_shelf(self):
        console.print("\n[yellow]▶ 获取书架数据...[/yellow]")
        shelf = self.wr.get_shelf()
        books = shelf.get("books", [])
        albums = shelf.get("albums", [])

        total = len(books)
        console.print(f"  书架共 [cyan]{len(books)}[/cyan] 本电子书，[cyan]{len(albums)}[/cyan] 个专辑")

        if not books:
            console.print("[dim]  书架为空，跳过[/dim]")
            return

        # 获取笔记本概览（含划线/想法统计）
        console.print("[yellow]▶ 获取笔记概览...[/yellow]")
        notebooks = self.wr.get_notebooks()
        notebook_map = {nb["bookId"]: nb for nb in notebooks}
        console.print(f"  共 [cyan]{len(notebooks)}[/cyan] 本书有笔记")

        # 同步每本书
        console.print(f"\n[yellow]▶ 同步书架（{total} 本）...[/yellow]")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("同步书架", total=total)

            for book_shelf in books:
                book_id = book_shelf["bookId"]
                book_title = book_shelf.get("title", book_id)
                progress.update(task, description=f"[cyan]{book_title[:20]}[/cyan]")

                # 增量检查
                last_read_ts = book_shelf.get("readUpdateTime", 0)
                state_key = f"book_{book_id}"
                if self.incremental and self.state.get(state_key) == last_read_ts and last_read_ts > 0:
                    progress.advance(task)
                    continue

                try:
                    book_info = self.wr.get_book_info(book_id)
                    progress_info = self.wr.get_book_progress(book_id)
                    nb_info = notebook_map.get(book_id)

                    page_id = self.ns.sync_book(book_info, progress_info, nb_info)

                    if (self.sync_highlights or self.sync_reviews) and nb_info:
                        has_notes = (
                            nb_info.get("noteCount", 0) > 0
                            or nb_info.get("reviewCount", 0) > 0
                        )
                        if has_notes:
                            notes = self.wr.get_book_notes(book_id)
                            self.ns.sync_book_notes(page_id, notes, book_title)

                    self.state[state_key] = last_read_ts

                except Exception as e:
                    console.print(f"\n  [red]✗ {book_title}: {e}[/red]")

                progress.advance(task)

        console.print("[green]✓ 书架同步完成[/green]")

    def _sync_stats(self):
        console.print("\n[yellow]▶ 同步阅读统计...[/yellow]")
        try:
            stats = self.wr.get_read_stats(mode="overall")
            self.ns.sync_stats(stats, "总计（全部历史）")
            console.print("[green]✓ 阅读统计同步完成[/green]")
        except Exception as e:
            console.print(f"[red]✗ 阅读统计同步失败：{e}[/red]")
