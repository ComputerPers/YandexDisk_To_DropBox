from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import datetime, timezone
from time import monotonic, sleep
from typing import Callable

from yd2dbx.classifier import FileClassifier
from yd2dbx.paths import filter_to_root, parent_folders

logger = logging.getLogger(__name__)
from yd2dbx.clients.dropbox import DropboxClient
from yd2dbx.clients.yandex_disk import YandexDiskClient
from yd2dbx.config import MigrationConfig
from yd2dbx.db import MigrationDB
from yd2dbx.models import ClassifiedEntry, DiffDecision, InventoryEntry, Provider, SyncOutcome
from yd2dbx.progress import (
    MultiLineProgress,
    format_eta,
    format_sync_progress,
    print_done,
    print_phase,
    print_stage,
)
from yd2dbx.reporting import write_reports
from yd2dbx.sync_runner import SyncRunner
from yd2dbx.transport import AuthenticationError


YANDEX_PAGE_SIZE = 10_000
YANDEX_WORKERS = 4
YANDEX_PAGES_PER_BATCH = 20
DROPBOX_WORKERS = 4
SYNC_INITIAL_WORKERS = 8
SYNC_MIN_WORKERS = 2
SYNC_MAX_WORKERS = 32
FOLDER_BATCH_SIZE = 1_000


class _AdaptiveThrottle:
    """Adaptive concurrency: −2 on rate-limit, +2 after clean streak."""

    def __init__(
        self,
        initial: int = SYNC_INITIAL_WORKERS,
        minimum: int = SYNC_MIN_WORKERS,
        maximum: int = SYNC_MAX_WORKERS,
        step: int = 2,
        increase_after: int = 20,
    ) -> None:
        self._lock = threading.Lock()
        self._current = initial
        self._minimum = minimum
        self._maximum = maximum
        self._step = step
        self._increase_after = increase_after
        self._ok_streak = 0
        self._total_429 = 0

    @property
    def current(self) -> int:
        with self._lock:
            return self._current

    @property
    def total_rate_limits(self) -> int:
        return self._total_429

    def on_rate_limit(self) -> int:
        """Called from transport threads on HTTP 429."""
        with self._lock:
            self._total_429 += 1
            self._ok_streak = 0
            old = self._current
            self._current = max(self._minimum, self._current - self._step)
            if self._current != old:
                logger.info("Rate limit → concurrency %d→%d (total 429s: %d)",
                            old, self._current, self._total_429)
            return self._current

    def on_success(self) -> int:
        """Called from main thread when a file transfer completes successfully."""
        with self._lock:
            self._ok_streak += 1
            if self._ok_streak >= self._increase_after and self._current < self._maximum:
                old = self._current
                self._current = min(self._maximum, self._current + self._step)
                self._ok_streak = 0
                logger.info("Clean streak → concurrency %d→%d", old, self._current)
            return self._current


class MigrationRunner:
    def __init__(
        self,
        *,
        config: MigrationConfig,
        db: MigrationDB,
        yandex: YandexDiskClient,
        dropbox: DropboxClient,
        classifier: FileClassifier,
        sync_runner: SyncRunner,
        confirm_fn: Callable[[str], str] = input,
        write_reports_fn: Callable[[str, str, dict[str, object]], object] = write_reports,
        print_phase_fn: Callable[[str, str], None] = print_phase,
        print_done_fn: Callable[[str, str], None] = print_done,
        print_stage_fn: Callable[[str, str], None] = print_stage,
    ) -> None:
        self.config = config
        self.db = db
        self.yandex = yandex
        self.dropbox = dropbox
        self.classifier = classifier
        self.sync_runner = sync_runner
        self.confirm_fn = confirm_fn
        self.write_reports_fn = write_reports_fn
        self.print_phase = print_phase_fn
        self.print_done = print_done_fn
        self.print_stage = print_stage_fn
        self._multi_progress: MultiLineProgress | None = None

    def run(self) -> int:
        phase = self.db.get_meta("phase", "init")
        logger.info("Starting migration, resuming from phase=%s", phase)
        if phase == "done":
            self.print_stage("Done", "Миграция уже завершена, повторный запуск не требуется")
            return 0

        if phase in {"init", "inventory", "yandex_inventory", "dropbox_inventory"}:
            self._check_read_access()
            self._run_inventory()
            phase = self.db.get_meta("phase", "init")

        if phase == "classify":
            self._classify_and_diff()
            phase = self.db.get_meta("phase", "init")

        if phase == "awaiting_confirm" and not self._confirm_sync():
            return 0

        if phase == "sync" or self.db.get_meta("phase", "init") == "sync":
            self._run_stage_check("Checks/Dropbox", "Проверяю право записи в Dropbox", self.dropbox.check_write_access)
            self._sync_files()
            self._write_final_report()
            self.db.set_meta("phase", "done")
            self.print_stage("Done", "Миграция завершена")

        return 0

    def _check_read_access(self) -> None:
        self._run_stage_check("Checks/Yandex", "Проверяю доступ к Yandex Disk", self.yandex.check_read_access)
        self._run_stage_check("Checks/Dropbox", "Проверяю доступ к Dropbox", self.dropbox.check_read_access)

    def _run_inventory(self) -> None:
        self.db.set_meta("phase", "inventory")

        yandex_done = self.db.get_meta("yandex_inventory_done", "0") == "1"
        dropbox_done = self.db.get_meta("dropbox_inventory_done", "0") == "1"

        if yandex_done and dropbox_done:
            self.db.set_meta("phase", "classify")
            return

        if not yandex_done:
            self.print_stage("Yandex", "Начинаю инвентаризацию Yandex Disk")
        if not dropbox_done:
            self.print_stage("Dropbox", "Начинаю инвентаризацию Dropbox")

        both_parallel = not yandex_done and not dropbox_done
        if both_parallel:
            self._multi_progress = MultiLineProgress(["Yandex", "Dropbox"])
        else:
            self._multi_progress = None

        with ThreadPoolExecutor(max_workers=2) as pool:
            top_futures: dict[object, str] = {}
            if not yandex_done:
                top_futures[pool.submit(self._fetch_yandex_parallel)] = "yandex"
            if not dropbox_done:
                top_futures[pool.submit(self._fetch_dropbox_parallel)] = "dropbox"

            for future in as_completed(top_futures):
                kind = top_futures[future]
                entries = future.result()
                filtered = self._filter_entries_to_root(entries)

                if kind == "yandex":
                    self.db.insert_inventory_page(Provider.YANDEX.value, filtered)
                    self.db.set_meta("yandex_inventory_done", "1")
                    count = self.db.count_inventory(Provider.YANDEX.value)
                    logger.info("Yandex inventory done: %d files (raw %d, filtered %d)",
                                count, len(entries), len(filtered))
                    if self._multi_progress:
                        self._multi_progress.finish("Yandex", f"{count} files collected")
                    else:
                        self.print_done("Yandex", f"{count} files collected")
                else:
                    self.db.insert_inventory_page(Provider.DROPBOX.value, filtered)
                    self.db.set_meta("dropbox_inventory_done", "1")
                    count = self.db.count_inventory(Provider.DROPBOX.value)
                    logger.info("Dropbox inventory done: %d files (raw %d, filtered %d)",
                                count, len(entries), len(filtered))
                    if self._multi_progress:
                        self._multi_progress.finish("Dropbox", f"{count} files collected")
                    else:
                        self.print_done("Dropbox", f"{count} files collected")

        if self._multi_progress:
            print()
        self._multi_progress = None

        self.db.set_meta("phase", "classify")

    def _fetch_yandex_parallel(self) -> list[InventoryEntry]:
        """Parallel inventory by adaptive offset batches — no file count limit."""
        all_entries: list[InventoryEntry] = []

        def fetch_page(offset: int) -> tuple[int, list[InventoryEntry]]:
            entries, _ = self.yandex.list_files_page(
                offset=offset, page_size=YANDEX_PAGE_SIZE,
            )
            return offset, entries

        def _report(count: int) -> None:
            if self._multi_progress:
                self._multi_progress.update("Yandex", f"{count} files")
            else:
                self.print_phase("Yandex", f"{count} files")

        frontier = 0
        with ThreadPoolExecutor(max_workers=YANDEX_WORKERS) as yd_pool:
            while True:
                offsets = [
                    frontier + i * YANDEX_PAGE_SIZE
                    for i in range(YANDEX_PAGES_PER_BATCH)
                ]
                futures = {yd_pool.submit(fetch_page, o): o for o in offsets}
                found_end = False
                for future in as_completed(futures):
                    _, entries = future.result()
                    if entries:
                        all_entries.extend(entries)
                        _report(len(all_entries))
                    if len(entries) < YANDEX_PAGE_SIZE:
                        found_end = True
                if found_end:
                    break
                frontier = offsets[-1] + YANDEX_PAGE_SIZE
        return all_entries

    def _fetch_dropbox_parallel(self) -> list[InventoryEntry]:
        root_files, top_folders = self.dropbox.list_folder_children("")
        all_entries = list(root_files)
        if not top_folders:
            return all_entries

        def list_one_folder(folder_path: str) -> list[InventoryEntry]:
            folder_entries: list[InventoryEntry] = []
            entries, cursor, has_more = self.dropbox.list_folder_start(folder_path)
            folder_entries.extend(entries)
            while has_more and cursor:
                entries, cursor, has_more = self.dropbox.list_folder_continue(cursor)
                folder_entries.extend(entries)
            return folder_entries

        def _report(count: int) -> None:
            if self._multi_progress:
                self._multi_progress.update("Dropbox", f"{count} files")
            else:
                self.print_phase("Dropbox", f"{count} files")

        workers = min(DROPBOX_WORKERS, len(top_folders))
        with ThreadPoolExecutor(max_workers=workers) as dbx_pool:
            futures = {
                dbx_pool.submit(list_one_folder, f): f for f in top_folders
            }
            for future in as_completed(futures):
                entries = future.result()
                all_entries.extend(entries)
                _report(len(all_entries))
        return all_entries

    def _classify_and_diff(self) -> None:
        self.print_stage("Classify", "Классифицирую файлы и строю diff")
        self.db.set_meta("phase", "classify")
        total = self.db.count_inventory(Provider.YANDEX.value)
        self.print_phase("Classify", f"0 / {total}")
        self.db.classify_yandex(
            self.classifier,
            progress_fn=lambda done: self.print_phase("Classify", f"{done} / {total}"),
        )
        self.print_done("Classify", f"{total} / {total}")
        logger.info("Classification done: %d files", total)
        self.print_stage("Diff", "Строю список файлов для переноса")
        sync_paths = [path for _, path in self.db.query_sync_candidates()]
        logger.info("Diff complete: %d files to sync", len(sync_paths))
        self.print_stage("Diff", f"Найдено {len(sync_paths)} файлов для синхронизации")
        self.db.init_sync_log(sync_paths)
        self.db.set_meta("phase", "awaiting_confirm")

    def _confirm_sync(self) -> bool:
        summary = self.db.query_diff_summary()
        pending_count = len(self.db.pending_sync_paths())
        self.print_stage("Confirm", "Жду подтверждение перед записью в Dropbox")
        response = (self.confirm_fn(self._confirmation_prompt(summary, pending_count)) or "").strip().lower()
        if response in {"", "y", "yes"}:
            self.db.set_meta("phase", "sync")
            return True
        self.db.set_meta("phase", "awaiting_confirm")
        return False

    def _sync_files(self) -> None:
        self.print_stage("Sync/Load", "Загружаю список файлов для переноса")
        raw_entries = self.db.pending_sync_entries()
        total = len(raw_entries)
        self.print_stage("Sync/Load", f"Найдено {total} файлов")

        if total == 0:
            self.print_stage("Sync", "Нечего переносить — все файлы синхронизированы")
            return

        paths = [entry.path for entry, _, _, _ in raw_entries]
        self.print_stage("Sync/Folders", "Вычисляю структуру папок")
        all_folders = parent_folders(paths)
        self.print_stage("Sync/Folders", f"Всего {len(all_folders)} папок")
        self._ensure_new_folders(all_folders)

        candidates = [
            (entry.path, self._candidate_from_entry(entry, cat, handling, reason))
            for entry, cat, handling, reason in raw_entries
        ]

        throttle = _AdaptiveThrottle()
        self._wire_rate_limit_callback(throttle)

        self.print_stage("Sync", f"Переношу {total} файлов в Dropbox (adaptive ×{throttle.current})")
        logger.info("Sync started: %d files, adaptive %d→%d workers",
                     total, SYNC_INITIAL_WORKERS, SYNC_MAX_WORKERS)
        done = 0
        synced = 0
        failed = 0
        t0 = monotonic()
        last_log_time = t0
        self.print_phase("Sync", format_sync_progress(0, total, 0, 0, 0))
        auth_error: AuthenticationError | None = None

        pending = list(reversed(candidates))
        in_flight: dict[object, str] = {}

        with ThreadPoolExecutor(max_workers=SYNC_MAX_WORKERS) as pool:
            def _submit_up_to_limit() -> None:
                while pending and len(in_flight) < throttle.current:
                    path, cand = pending.pop()
                    fut = pool.submit(self.sync_runner.transfer_one, cand)
                    in_flight[fut] = path

            _submit_up_to_limit()

            while in_flight:
                finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    path = in_flight.pop(future)
                    try:
                        outcome = future.result()
                    except AuthenticationError as exc:
                        auth_error = exc
                        for f in list(in_flight):
                            f.cancel()
                        in_flight.clear()
                        pending.clear()
                        break
                    except Exception as exc:
                        outcome = SyncOutcome(
                            path=path, status="review_required", attempts=0, detail=str(exc),
                        )
                    self.db.record_sync_outcome(
                        outcome.path, outcome.status, outcome.attempts, outcome.detail,
                    )
                    done += 1
                    if outcome.status == "synced":
                        synced += 1
                        throttle.on_success()
                    else:
                        failed += 1
                    elapsed = monotonic() - t0
                    progress = format_sync_progress(done, total, synced, failed, elapsed)
                    self.print_phase("Sync", f"{progress}  [×{throttle.current}]")
                    now = monotonic()
                    if done % 100 == 0 or now - last_log_time >= 60:
                        logger.info("Progress: %d/%d ok:%d err:%d ×%d elapsed:%s",
                                    done, total, synced, failed,
                                    throttle.current, format_eta(elapsed))
                        last_log_time = now

                if auth_error is not None:
                    break
                _submit_up_to_limit()

        elapsed = monotonic() - t0
        if auth_error is not None:
            self.print_stage(
                "Sync",
                f"ОСТАНОВЛЕНО после {done}/{total} — ok:{synced}, ошибок:{failed}, за {format_eta(elapsed)}",
            )
            raise AuthenticationError(
                f"Токен истёк: {auth_error}. "
                f"Перенесено {synced} из {total} файлов. "
                f"Обновите токен и запустите ./yd2dbx run для продолжения."
            ) from auth_error
        logger.info("Sync complete: %d/%d synced, %d failed, 429s: %d, elapsed %s",
                     synced, total, failed, throttle.total_rate_limits, format_eta(elapsed))
        self.print_done("Sync", f"{total} / {total} — ok:{synced}, ошибок:{failed}, "
                        f"429×{throttle.total_rate_limits}, за {format_eta(elapsed)}")

    def _wire_rate_limit_callback(self, throttle: _AdaptiveThrottle) -> None:
        """Connect the Dropbox transport's 429 signal to the adaptive throttle."""
        client = getattr(self.sync_runner, "dropbox_client", None)
        if client is None:
            return
        transport = getattr(client, "transport", None)
        if transport is not None and hasattr(transport, "on_rate_limit"):
            transport.on_rate_limit = throttle.on_rate_limit

    def _ensure_new_folders(self, all_folders: list[str]) -> None:
        if not all_folders:
            return
        if self.db.get_meta("folders_done", "0") == "1":
            self.print_stage("Folders", f"Папки уже созданы ({len(all_folders)} шт, пропускаю)")
            return

        known = self.db.known_dropbox_folders()
        new_folders = [f for f in all_folders if f.lower() not in known]
        skipped = len(all_folders) - len(new_folders)

        if not new_folders:
            self.print_stage("Folders", f"Все {skipped} папок уже существуют в Dropbox")
            self.db.set_meta("folders_done", "1")
            return

        by_depth: dict[int, list[str]] = {}
        for folder in new_folders:
            depth = folder.count("/")
            by_depth.setdefault(depth, []).append(folder)

        done_raw = self.db.get_meta("folders_done_depths", "")
        done_depths: set[int] = {int(d) for d in done_raw.split(",") if d}
        already_created = sum(len(by_depth[d]) for d in done_depths if d in by_depth)
        remaining = {d: fs for d, fs in by_depth.items() if d not in done_depths}
        remaining_count = sum(len(fs) for fs in remaining.values())

        if not remaining:
            self.print_stage("Folders", f"Все {len(new_folders)} новых папок уже созданы")
            self.db.set_meta("folders_done", "1")
            return

        total_new = len(new_folders)
        self.print_stage(
            "Folders",
            f"Создаю {remaining_count} папок ({skipped + already_created} пропущено)",
        )

        created = already_created
        for depth in sorted(remaining):
            depth_folders = remaining[depth]
            for i in range(0, len(depth_folders), FOLDER_BATCH_SIZE):
                batch = depth_folders[i : i + FOLDER_BATCH_SIZE]
                self._create_folder_batch_with_poll(
                    batch,
                    lambda msg: self.print_phase("Folders", f"{created} / {total_new} — {msg}"),
                )
                created += len(batch)
                self.print_phase("Folders", f"{created} / {total_new}")
            done_depths.add(depth)
            self.db.set_meta("folders_done_depths", ",".join(str(d) for d in sorted(done_depths)))

        self.print_done("Folders", f"{total_new} / {total_new}")
        self.db.set_meta("folders_done", "1")

    def _create_folder_batch_with_poll(
        self,
        paths: list[str],
        progress_fn: Callable[[str], None] | None = None,
    ) -> None:
        job_id = self.dropbox.create_folder_batch(paths)
        if job_id is None:
            return
        for attempt in range(1, 121):
            status = self.dropbox.check_folder_batch_job(job_id)
            if status != "in_progress":
                return
            if progress_fn:
                progress_fn(f"batch {len(paths)} шт, ожидание {attempt}с")
            sleep(1)
        raise RuntimeError(f"Folder batch job {job_id} timed out")

    def _write_final_report(self) -> None:
        self.print_stage("Report", "Сохраняю итоговый отчёт")
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": self.db.query_diff_summary(),
            "diff_items": self.db.query_diff_items(),
            "sync_outcomes": self.db.list_sync_outcomes(),
        }
        self.write_reports_fn(self.config.report_dir, "run", payload)

    @staticmethod
    def _candidate_from_entry(
        entry: InventoryEntry,
        category: str = "document",
        handling: str = "sync",
        reason: str = "document in primary migration wave",
    ) -> DiffDecision:
        classified = ClassifiedEntry(
            entry=entry,
            category=category,
            handling=handling,
            reason=reason,
        )
        return DiffDecision(entry=classified, status="missing_in_dropbox", reason="path absent in Dropbox")

    @staticmethod
    def _confirmation_prompt(summary: dict[str, int], pending_count: int) -> str:
        parts = [f"{key}={value}" for key, value in sorted(summary.items())]
        details = ", ".join(parts)
        return f"Перенести {pending_count} файлов в Dropbox? [{details}] [Y/n] "

    def _filter_entries_to_root(self, entries: list[InventoryEntry]) -> list[InventoryEntry]:
        return filter_to_root(entries, self.config.root_path)

    def _run_stage_check(self, phase: str, detail: str, check_fn: Callable[[], None]) -> None:
        self.print_stage(phase, detail)
        try:
            check_fn()
        except Exception as exc:
            self.print_stage(phase, f"ERROR: {exc}")
            raise
        self.print_stage(phase, "OK")

    _parent_folders = staticmethod(parent_folders)
