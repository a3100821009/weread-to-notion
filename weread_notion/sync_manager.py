"""
主同步流程：协调微信阅读和 Notion 两侧的数据同步
"""

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from weread_notion.weread_client import WeReadClient
from weread_notion.notion_syncer import NotionSyncer
from weread_notion.common import extract_reading_time, seconds_to_hm, retry_on_failure
from notion_client.errors import APIResponseError

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


def push_assets_to_github():
    """将 covers/ 和 sync_state.json 的变更提交并推送到 GitHub"""
    covers_dir = Path("covers")
    has_covers = covers_dir.exists() and any(covers_dir.iterdir())

    try:
        # 配置 git（GitHub Actions 环境）
        subprocess.run(
            ["git", "config", "user.name", "WeRead Sync Bot"],
            capture_output=True, timeout=10,
        )
        subprocess.run(
            ["git", "config", "user.email", "sync@weread-to-notion.local"],
            capture_output=True, timeout=10,
        )
        # 添加封面 + 同步状态
        add_cmd = ["git", "add", "sync_state.json"]
        if has_covers:
            add_cmd.append("covers/")
        subprocess.run(add_cmd, capture_output=True, timeout=10)
        # 检查是否有变更
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            return  # 无变更

        # 提交并推送到 GitHub（使用 HEAD:branch 绕过 detached HEAD 限制）
        branch = os.environ.get("GITHUB_REF_NAME", "main")
        subprocess.run(
            ["git", "commit", "-m", "Update covers & sync state"],
            capture_output=True, timeout=10,
        )
        push_result = subprocess.run(
            ["git", "push", "origin", f"HEAD:refs/heads/{branch}"],
            capture_output=True, timeout=60,
        )
        if push_result.returncode != 0:
            stderr = push_result.stderr.decode()[:300]
            console.print(f"[red]✗ 推送失败: {stderr}[/red]")
            return
        console.print(f"[green]✓ 素材已推送到 GitHub ({branch})[/green]")
    except Exception as e:
        console.print(f"[dim]推送异常: {e}[/dim]")


class SyncManager:
    def __init__(
        self,
        weread_key: str,
        notion_token: str,
        parent_page_id: str,
        sync_highlights: bool = True,
        sync_reviews: bool = True,
        request_delay: float = 0,
        incremental: bool = True,
    ):
        self.wr = WeReadClient(weread_key, request_delay)
        self.sync_highlights = sync_highlights
        self.sync_reviews = sync_reviews
        self.incremental = incremental
        self.state = load_state()
        self.ns = NotionSyncer(
            notion_token, parent_page_id,
            book_pages=self.state.get("book_pages", {}),
            shelf_db_id=self.state.get("shelf_db_id"),
        )
        # 初始化 book_meta（追踪每本书的阅读时间等信息）
        if "book_meta" not in self.state:
            self.state["book_meta"] = {}

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
                reading_hm = seconds_to_hm(reading_time)
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
        max_attempts = 10  # 最多重试 10 次（~100 分钟）
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                console.print(f"[yellow]⏳ Notion API 限流，等待 10 分钟后第 {attempt} 次重试...[/yellow]")
                time.sleep(600)

            try:
                self._run_once()
                return  # 成功，退出重试循环
            except APIResponseError as e:
                err_msg = str(e)
                if "429" not in err_msg and "rate limited" not in err_msg.lower():
                    raise  # 非 429 错误直接抛出不重试
                if attempt == max_attempts:
                    console.print(f"[red]✗ 已重试 {max_attempts} 次仍遇到限流，放弃本次同步[/red]")
                    raise

    def _run_once(self):
        console.print(Panel.fit(
            "[bold cyan]微信阅读 → Notion 同步[/bold cyan]\n"
            "[dim]WeRead to Notion Sync Tool[/dim]",
            border_style="cyan"
        ))

        # 初始化 Notion 结构
        console.print("\n[yellow]▶ 初始化 Notion 结构...[/yellow]")
        self.ns.setup()
        console.print("[green]✓ Notion 结构就绪[/green]")

        # 1. 同步书架（不再封面下载，统一在 _sync_all_covers 处理）
        self._sync_shelf()

        # 2. 检测并清理书架中已移除的低阅读时间书籍
        #    已在 _sync_shelf 末尾调用 _cleanup_removed_books

        # 回存 book_pages 映射 + book_meta + 书架数据库 ID
        self.state["book_pages"] = self.ns._book_pages
        self.state["book_meta"] = self.state.get("book_meta", {})
        if self.ns._shelf_db_id:
            self.state["shelf_db_id"] = self.ns._shelf_db_id
        save_state(self.state)

        # 最终确认
        synced_books = len(self.ns._book_pages)
        if synced_books == 0:
            console.print("\n[yellow]⚠ 书架数据库中当前没有任何书籍记录。[/yellow]")
            console.print("  请检查 GitHub Actions 运行日志中的书架同步汇总，或确认 Notion 集成权限正常。")

        # 4. 等待封面下载完成 → 推送 GitHub → 更新 Notion
        self._sync_all_covers()

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
        success_count = 0
        fail_count = 0
        skip_count = 0
        first_error = None

        # 精细增量跳过：从书架数据提取阅读时长，与 state 比对。
        # 阅读时长未变 → 书没被读过，0 个 API 调用直接跳过。
        sync_books = []
        for book_shelf in books:
            book_id = book_shelf["bookId"]
            book_title = book_shelf.get("title", book_id)

            if self.incremental:
                existing_meta = self.state.get("book_meta", {}).get(book_id, {})

                # 新书（不在之前 state 中）→ 需要同步
                if not existing_meta:
                    sync_books.append(book_shelf)
                    continue

                # 用 readUpdateTime 做增量比对（书架数据自带，无需调 API）。
                # 之前的实现用 extract_reading_time(book_shelf, {}) 提取
                # current_rt，与 stored_rt（上次用 book_shelf + progress_info
                # 合并提取）口径不一致，导致大量本应跳过的书被误判为"阅读时长变了"。
                current_rtu = book_shelf.get("readUpdateTime", 0)
                stored_rtu = existing_meta.get("readUpdateTime", 0)
                nb_info = notebook_map.get(book_id)

                if current_rtu == stored_rtu:
                    # readUpdateTime 未变 → 数据无变化，跳过，零 API 调用
                    skip_count += 1
                    shelf_cover = book_shelf.get("cover", "")
                    cover_url = shelf_cover or existing_meta.get("coverUrl", "")
                    self.state.setdefault("book_meta", {})[book_id] = {
                        "title": book_title,
                        "author": existing_meta.get("author", ""),
                        "readingTime": existing_meta.get("readingTime", 0),
                        "coverUrl": cover_url,
                        "noteCount": existing_meta.get("noteCount", 0),
                        "reviewCount": existing_meta.get("reviewCount", 0),
                        "progress": existing_meta.get("progress", 0),
                        "startDate": existing_meta.get("startDate", ""),
                        "readUpdateTime": stored_rtu,
                        "lastSynced": datetime.now().isoformat(),
                    }
                else:
                    # readUpdateTime 变了 → 书被读过/笔记有更新，需要同步
                    sync_books.append(book_shelf)
            else:
                # 全量模式：所有书都处理
                sync_books.append(book_shelf)

        if skip_count > 0:
            console.print(f"  [dim]增量跳过 {skip_count} 本（阅读时长未变，零 API 调用）[/dim]")
        if sync_books:
            console.print(f"  并发同步 [cyan]{len(sync_books)}[/cyan] 本（10 线程）...")

            # 月度阅读数据是所有书共享的（API 返回跨书汇总），只调一次
            shared_read_detail = self.wr.get_book_read_detail("")
            # 偶发性空数据，重试一次
            if not shared_read_detail.get("readDays") and not shared_read_detail.get("readRecords"):
                import time
                time.sleep(1)
                shared_read_detail = self.wr.get_book_read_detail("")
            read_days = shared_read_detail.get("readDays", 0)
            records_count = len(shared_read_detail.get("readRecords", []))
            console.print(f"  [dim]月度阅读数据: {read_days}天, {records_count}条记录[/dim]")

            def sync_one(book_shelf: dict) -> tuple:
                """在线程中同步单本书，返回 (book_id, 是否成功, 错误信息)"""
                book_id = book_shelf["bookId"]
                book_title = book_shelf.get("title", book_id)
                book_info = self.wr.get_book_info(book_id)
                progress_info = self.wr.get_book_progress(book_id)
                nb_info = notebook_map.get(book_id)
                cover_url = book_info.get("cover", "")
                author = book_info.get("author", "")
                reading_time = extract_reading_time(book_shelf, progress_info)
                note_count = nb_info.get("noteCount", 0) if nb_info else 0
                review_count = nb_info.get("reviewCount", 0) if nb_info else 0

                # 提取阅读进度（0-100）和首次阅读日期
                progress_raw = 0
                start_date = ""
                if progress_info and progress_info.get("book"):
                    p = progress_info["book"]
                    progress_raw = p.get("progress", 0)
                    for fld in ["firstReadTime", "firstOpenTime", "createTime"]:
                        ft = p.get(fld, 0)
                        if ft and ft > 0:
                            start_date = WeReadClient.ts_to_date(ft)
                            break
                if not start_date:
                    for fld in ["createTime", "addTime"]:
                        ft = book_info.get(fld, 0)
                        if ft and ft > 0:
                            start_date = WeReadClient.ts_to_date(ft)
                            break
                if not start_date and book_shelf:
                    for fld in ["createTime", "addTime", "readUpdateTime"]:
                        ft = book_shelf.get(fld, 0)
                        if ft and ft > 0:
                            start_date = WeReadClient.ts_to_date(ft)
                            break

                # 书本阅读详情——所有书共享月度汇总数据
                book_read_detail = shared_read_detail

                self.state.setdefault("book_meta", {})[book_id] = {
                    "title": book_title,
                    "author": author,
                    "readingTime": reading_time, "coverUrl": cover_url,
                    "noteCount": note_count, "reviewCount": review_count,
                    "progress": progress_raw, "startDate": start_date,
                    "readUpdateTime": book_shelf.get("readUpdateTime", 0),
                    "lastSynced": datetime.now().isoformat(),
                }

                try:
                    page_id = self.ns.sync_book(book_info, progress_info, nb_info, book_shelf, book_read_detail)

                    # readingTime 变了 → 更新全部页面内容（笔记/划线/评价/统计）
                    if self.sync_highlights or self.sync_reviews:
                        if nb_info:
                            notes = self.wr.get_book_notes(book_id)
                            social = None
                            try:
                                social = self.wr.get_book_social_notes(book_id)
                            except Exception:
                                pass
                        else:
                            notes = {"highlights": [], "reviews": [], "chapters": {}}
                            social = None
                        self.ns.sync_book_notes(page_id, notes, social, book_title,
                                                 book_info, book_read_detail, progress_info,
                                                 book_id=book_id, start_date=start_date)

                    self.state[f"book_{book_id}"] = book_shelf.get("readUpdateTime", 0)
                    return (book_id, True, None)
                except Exception as e:
                    # 429 限流 → 全域重试，不由线程捕获
                    err_msg = str(e)
                    if "429" in err_msg or "rate limited" in err_msg.lower():
                        raise
                    return (book_id, False, (book_title, type(e).__name__, err_msg))

            with ThreadPoolExecutor(max_workers=15) as pool:
                future_map = {pool.submit(sync_one, bs): bs["bookId"] for bs in sync_books}
                try:
                    for future in as_completed(future_map):
                        bid, ok, err = future.result()
                        if ok:
                            success_count += 1
                        else:
                            fail_count += 1
                            if first_error is None:
                                first_error = err
                            title, etype, msg = err
                            console.print(f"\n  [red]✗ {title[:20]}: {etype}: {msg[:80]}[/red]")
                except APIResponseError as e:
                    # 429 全域重试：关闭线程池，抛给 run() 处理
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise

        console.print(f"\n[bold]书架同步汇总：[/bold] 成功 [green]{success_count}[/green] | 失败 [red]{fail_count}[/red] | 跳过 [dim]{skip_count}[/dim]")
        if fail_count == total and total > 0:
            console.print(f"[bold red]⚠ 所有书籍同步失败！[/bold red]")
            if first_error:
                console.print(f"[red]  首个错误：{first_error[0]} → {first_error[1]}: {first_error[2]}[/red]")
        elif success_count == 0 and total > 0:
            console.print("[yellow]⚠ 没有新书被同步（可能全部被增量跳过）[/yellow]")

        console.print("[green]✓ 书架同步完成[/green]")

        # 清理已从书架移除的低阅读时间书籍
        self._cleanup_removed_books(current_book_ids)

    def _sync_all_covers(self):
        """从 book_meta 读取 coverUrl，并发下载封面 → 推送 GitHub → 更新 Notion"""
        from weread_notion.notion_syncer import _persist_cover

        book_meta = self.state.get("book_meta", {})
        if not book_meta:
            console.print("\n[dim]▶ 封面：无书籍记录，跳过[/dim]")
            return

        # 收集所有需要下载的封面
        pending: dict[str, str] = {}
        have = 0
        for book_id, meta in book_meta.items():
            url = meta.get("coverUrl", "")
            if not url:
                continue
            cover_path = Path("covers") / f"{book_id}.jpg"
            if cover_path.exists():
                have += 1
                continue
            pending[book_id] = url

        console.print(f"\n[yellow]▶ 下载封面...[/yellow] (需下载 [cyan]{len(pending)}[/cyan] | 已有 [dim]{have}[/dim])")

        success = 0
        if pending:
            def _download_one(bid: str, url: str) -> bool:
                return bool(_persist_cover(bid, url))

            with ThreadPoolExecutor(max_workers=10) as pool:
                future_map = {
                    pool.submit(_download_one, bid, url): bid
                    for bid, url in pending.items()
                }
                for i, future in enumerate(as_completed(future_map), 1):
                    if future.result():
                        success += 1
                    if i % 20 == 0 or i == len(pending):
                        console.print(f"  封面下载进度: [green]{success}[/green]/{len(pending)}")

            if success == 0 and have == 0:
                console.print("[red]✦ 所有封面下载失败！可能 WeRead 封面 URL 已过期或网络问题[/red]")
                return

            console.print(f"  [green]✓ 封面下载完成: {success}/{len(pending)}[/green]")

        # 推送到 GitHub
        if success > 0 or have > 0:
            push_assets_to_github()

        # 更新 Notion 页面封面
        updated = self.ns.update_page_covers()
        if updated > 0:
            console.print(f"  [green]✓ Notion 封面已更新 {updated} 本[/green]")
        else:
            console.print("  [dim]封面均就绪，无需更新[/dim]")


