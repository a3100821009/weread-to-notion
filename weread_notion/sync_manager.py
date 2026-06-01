"""
主同步流程：协调微信阅读和 Notion 两侧的数据同步
"""

import json
import os
from datetime import datetime
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
        # 初始化 book_meta（追踪每本书的阅读时间等信息）
        if "book_meta" not in self.state:
            self.state["book_meta"] = {}

    @staticmethod
    def _extract_reading_time(book_shelf: dict, progress_info: dict) -> int:
        """
        从可用数据中提取阅读时间（秒），返回 0 表示无法确定。
        尝试多个可能的字段名以兼容不同 API 版本。
        """
        # 尝试从书架数据获取
        for field in ["readTime", "readingTime", "totalReadTime", "readDuration"]:
            val = book_shelf.get(field, 0)
            if val and val > 0:
                return int(val)

        # 尝试从进度数据获取
        book_prog = (progress_info or {}).get("book", {})
        for field in ["readTime", "readingTime", "totalReadTime", "duration"]:
            val = book_prog.get(field, 0)
            if val and val > 0:
                return int(val)

        # 兜底：从阅读进度估算（进度>0但无精确时间，保守处理）
        prog = book_prog.get("progress", 0)
        if prog > 0:
            # 有进度但无精确时间，设为 -1 标记"已读但时间未知"
            return -1

        return 0  # 无法确定

    def _cleanup_removed_books(self, current_book_ids: set[str]):
        """
        检测书架中已移除的书籍，阅读时间 < 10分钟（600秒）则同步删除 Notion 页面。
        无法确定阅读时间的书籍保守保留。
        """
        book_meta = self.state.get("book_meta", {})
        book_pages = self.state.get("book_pages", {})
        removed_ids = set(book_meta.keys()) - current_book_ids

        if not removed_ids:
            return

        # 安全检查：如果超过 30% 的书籍被检测为"移除"，很可能是 API 异常
        # 此时跳过清理，防止误删
        total_known = len(book_meta)
        if total_known > 0 and len(removed_ids) > total_known * 0.3:
            console.print(
                f"\n[yellow]⚠ 检测到 {len(removed_ids)}/{total_known} 本书从书架消失（> 30%），"
                f"可能为 API 异常，已跳过清理保护数据安全[/yellow]"
            )
            return

        console.print(f"\n[yellow]▶ 检测到 {len(removed_ids)} 本书已从书架移除，检查是否需要清理...[/yellow]")

        deleted_count = 0
        kept_count = 0

        for book_id in list(removed_ids):
            meta = book_meta.get(book_id, {})
            reading_time = meta.get("readingTime", 0)
            title = meta.get("title", book_id)
            notion_page_id = book_pages.get(book_id)

            if reading_time < 0:
                # 已读但无法确定精确时间，保守保留
                console.print(f"  [dim]⏭ 保留（已读但无法确定时间）: {title}[/dim]")
                kept_count += 1
            elif 0 < reading_time < 600:
                # 阅读时间不足 10 分钟 → 删除
                if notion_page_id:
                    success = self.ns.delete_book_page(notion_page_id, book_id)
                    if success:
                        console.print(f"  [yellow]🗑 已删除: {title}（阅读 {reading_time} 秒）[/yellow]")
                    else:
                        console.print(f"  [dim]🗑 已清理: {title}（页面可能已被手动删除）[/dim]")
                else:
                    console.print(f"  [dim]🗑 已清理记录: {title}[/dim]")
                deleted_count += 1
            elif reading_time >= 600:
                # 阅读超过 10 分钟 → 保留
                reading_hm = WeReadClient.seconds_to_hm(reading_time)
                console.print(f"  [dim]⏭ 保留（已读 {reading_hm}）: {title}[/dim]")
                kept_count += 1
            else:
                # reading_time == 0，无法确定 → 保守保留
                console.print(f"  [dim]⏭ 保留（无法确定阅读时间）: {title}[/dim]")
                kept_count += 1

            # 清理 book_meta 记录（无论是否删除页面）
            del book_meta[book_id]

        if deleted_count > 0:
            console.print(f"  [green]✓ 清理完成：删除 {deleted_count} 本，保留 {kept_count} 本[/green]")

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

        # 2. 检测并清理书架中已移除的低阅读时间书籍
        #    必须在 _sync_shelf 之后，因为它收集了 current_book_ids
        #    但不能在 _sync_shelf 内部，因为需要先完成所有书籍的同步
        #    self._cleanup_removed_books 已在 _sync_shelf 末尾调用

        # 3. 同步阅读统计
        if self.sync_stats:
            self._sync_stats()

        # 回存 book_pages 映射 + book_meta
        self.state["book_pages"] = self.ns._book_pages
        self.state["book_meta"] = self.state.get("book_meta", {})
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

        # 收集当前书架的所有 bookId（用于后续检测已移除书籍）
        current_book_ids = {b["bookId"] for b in books}

        # 获取笔记本概览（含划线/想法统计）
        console.print("[yellow]▶ 获取笔记概览...[/yellow]")
        notebooks = self.wr.get_notebooks()
        # bookId -> notebook entry
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

                # 增量检查：最近阅读时间是否有变化
                last_read_ts = book_shelf.get("readUpdateTime", 0)
                state_key = f"book_{book_id}"
                if self.incremental and self.state.get(state_key) == last_read_ts and last_read_ts > 0:
                    progress.advance(task)
                    continue

                try:
                    # 获取完整书籍信息
                    book_info = self.wr.get_book_info(book_id)
                    progress_info = self.wr.get_book_progress(book_id)
                    nb_info = notebook_map.get(book_id)

                    # 提取并存储阅读时间
                    reading_time = self._extract_reading_time(book_shelf, progress_info)
                    self.state.setdefault("book_meta", {})[book_id] = {
                        "title": book_title,
                        "readingTime": reading_time,
                        "lastSynced": datetime.now().isoformat(),
                    }

                    # 同步到 Notion 书架数据库
                    page_id = self.ns.sync_book(book_info, progress_info, nb_info)

                    # 同步划线 + 想法（自己的 + 社交笔记）
                    if (self.sync_highlights or self.sync_reviews) and nb_info:
                        has_notes = (
                            nb_info.get("noteCount", 0) > 0
                            or nb_info.get("reviewCount", 0) > 0
                        )
                        if has_notes:
                            notes = self.wr.get_book_notes(book_id)

                            # 同步获取社交笔记（热门划线 + 评论）
                            social = None
                            try:
                                social = self.wr.get_book_social_notes(book_id)
                            except Exception:
                                pass

                            self.ns.sync_book_notes(page_id, notes, social, book_title)

                    # 保存状态
                    self.state[state_key] = last_read_ts

                except Exception as e:
                    console.print(f"\n  [red]✗ {book_title}: {e}[/red]")

                progress.advance(task)

        console.print("[green]✓ 书架同步完成[/green]")

        # 清理已从书架移除的低阅读时间书籍
        self._cleanup_removed_books(current_book_ids)

    def _sync_stats(self):
        console.print("\n[yellow]▶ 同步阅读统计...[/yellow]")
        try:
            stats = self.wr.get_read_stats(mode="overall")
            self.ns.sync_stats(stats, "总计（全部历史）")
            console.print("[green]✓ 阅读统计同步完成[/green]")
        except Exception as e:
            console.print(f"[red]✗ 阅读统计同步失败：{e}[/red]")
