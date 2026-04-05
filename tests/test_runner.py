import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from yd2dbx.classifier import FileClassifier
from yd2dbx.config import MigrationConfig
from yd2dbx.db import MigrationDB
from yd2dbx.models import InventoryEntry, Provider, SyncOutcome
from yd2dbx.runner import MigrationRunner
from yd2dbx.transport import AuthenticationError


def make_entry(
    provider: Provider,
    path: str,
    *,
    size: int = 128,
    modified: str | None = "2024-01-01T00:00:00+00:00",
    mime_type: str | None = "application/pdf",
) -> InventoryEntry:
    return InventoryEntry(
        provider=provider,
        path=path,
        size=size,
        modified=modified,
        mime_type=mime_type,
        source_hash="hash",
        source_hash_type="md5" if provider == Provider.YANDEX else "content_hash",
    )


class FakeYandexClient:
    def __init__(
        self,
        pages: dict[int, tuple[list[InventoryEntry], bool]] | None = None,
        *,
        check_error: Exception | None = None,
    ) -> None:
        self.pages = pages or {}
        self.list_calls: list[int] = []
        self.read_checks = 0
        self.check_error = check_error

    def check_read_access(self) -> None:
        if self.check_error is not None:
            raise self.check_error
        self.read_checks += 1

    def list_files_page(
        self, offset: int = 0, page_size: int = 1000, media_type: str | None = None,
    ) -> tuple[list[InventoryEntry], bool]:
        self.list_calls.append(offset)
        return self.pages.get(offset, ([], False))



class FakeDropboxClient:
    def __init__(
        self,
        *,
        start_result: tuple[list[InventoryEntry], str | None, bool] | None = None,
        continue_results: dict[str, tuple[list[InventoryEntry], str | None, bool]] | None = None,
        children_result: tuple[list[InventoryEntry], list[str]] | None = None,
        folder_start_results: dict[str, tuple[list[InventoryEntry], str | None, bool]] | None = None,
        check_error: Exception | None = None,
    ) -> None:
        self.start_result = start_result or ([], None, False)
        self.continue_results = continue_results or {}
        self.children_result = children_result
        self.folder_start_results = folder_start_results or {}
        self.start_calls = 0
        self.continue_calls: list[str] = []
        self.read_checks = 0
        self.write_checks = 0
        self.ensured_folders: list[str] = []
        self.check_error = check_error

    def check_read_access(self) -> None:
        if self.check_error is not None:
            raise self.check_error
        self.read_checks += 1

    def check_write_access(self) -> None:
        self.write_checks += 1

    def ensure_folders(self, folders: list[str]) -> None:
        self.ensured_folders.extend(folders)

    def create_folder_batch(self, paths: list[str]) -> str | None:
        self.ensured_folders.extend(paths)
        return None

    def check_folder_batch_job(self, job_id: str) -> str:
        return "complete"

    def list_folder_children(self, path: str = "") -> tuple[list[InventoryEntry], list[str]]:
        if self.children_result is not None:
            return self.children_result
        return self.start_result[0], []

    def list_folder_start(self, root_path: str = "") -> tuple[list[InventoryEntry], str | None, bool]:
        self.start_calls += 1
        if root_path and root_path in self.folder_start_results:
            return self.folder_start_results[root_path]
        return self.start_result

    def list_folder_continue(self, cursor: str) -> tuple[list[InventoryEntry], str | None, bool]:
        self.continue_calls.append(cursor)
        return self.continue_results[cursor]


class FakeSyncRunner:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def transfer_one(self, candidate) -> SyncOutcome:
        path = candidate.entry.entry.path
        self.paths.append(path)
        return SyncOutcome(path=path, status="synced", attempts=1, detail="server-side transfer completed")


class MigrationRunnerTests(unittest.TestCase):
    def test_run_executes_full_cycle_and_writes_report(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            yandex = FakeYandexClient(
                pages={
                    0: (
                        [
                            make_entry(Provider.YANDEX, "/docs/project/missing.pdf", size=10),
                            make_entry(Provider.YANDEX, "/docs/match.pdf", size=20),
                        ],
                        False,
                    )
                }
            )
            dropbox = FakeDropboxClient(
                start_result=(
                    [make_entry(Provider.DROPBOX, "/docs/match.pdf", size=20)],
                    None,
                    False,
                )
            )
            sync_runner = FakeSyncRunner()
            prompts: list[str] = []
            reports: list[tuple[str, str, dict[str, object]]] = []
            stage_messages: list[tuple[str, str]] = []
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(MigrationConfig.from_env({})),
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: prompts.append(prompt) or "",
                write_reports_fn=lambda report_dir, report_name, payload: reports.append((report_dir, report_name, payload)),
                print_stage_fn=lambda phase, detail: stage_messages.append((phase, detail)),
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(yandex.read_checks, 1)
            self.assertEqual(dropbox.read_checks, 1)
            self.assertEqual(dropbox.write_checks, 1)
            self.assertEqual(dropbox.ensured_folders, ["/docs/project"])
            self.assertEqual(sync_runner.paths, ["/docs/project/missing.pdf"])
            self.assertEqual(db.get_meta("phase"), "done")
            self.assertEqual(len(prompts), 1)
            self.assertEqual(reports[0][1], "run")
            self.assertEqual(reports[0][2]["summary"]["missing_in_dropbox"], 1)
            self.assertEqual(len(reports[0][2]["sync_outcomes"]), 1)
            self.assertIn(("Checks/Yandex", "Проверяю доступ к Yandex Disk"), stage_messages)
            self.assertIn(("Checks/Yandex", "OK"), stage_messages)
            self.assertIn(("Checks/Dropbox", "Проверяю доступ к Dropbox"), stage_messages)
            self.assertIn(("Checks/Dropbox", "OK"), stage_messages)
            self.assertIn(("Yandex", "Начинаю инвентаризацию Yandex Disk"), stage_messages)
            self.assertIn(("Dropbox", "Начинаю инвентаризацию Dropbox"), stage_messages)
            self.assertIn(("Classify", "Классифицирую файлы и строю diff"), stage_messages)
            self.assertIn(("Diff", "Найдено 1 файлов для синхронизации"), stage_messages)
            self.assertIn(("Confirm", "Жду подтверждение перед записью в Dropbox"), stage_messages)
            self.assertIn(("Checks/Dropbox", "Проверяю право записи в Dropbox"), stage_messages)
            self.assertIn(("Sync/Load", "Найдено 1 файлов"), stage_messages)
            self.assertIn(("Folders", "Создаю 1 папок (1 пропущено)"), stage_messages)
            self.assertIn(("Report", "Сохраняю итоговый отчёт"), stage_messages)
            self.assertIn(("Done", "Миграция завершена"), stage_messages)
            db.close()

    def test_run_reports_check_error_per_provider(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            stage_messages: list[tuple[str, str]] = []
            yandex = FakeYandexClient(check_error=RuntimeError("network down"))
            dropbox = FakeDropboxClient(start_result=([], None, False))
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(MigrationConfig.from_env({})),
                sync_runner=sync_runner,
                print_stage_fn=lambda phase, detail: stage_messages.append((phase, detail)),
            )

            with self.assertRaises(RuntimeError):
                runner.run()

            self.assertEqual(
                stage_messages,
                [
                    ("Checks/Yandex", "Проверяю доступ к Yandex Disk"),
                    ("Checks/Yandex", "ERROR: network down"),
                ],
            )
            db.close()

    def test_run_resumes_partial_sync_without_reconfirming(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            classifier = FileClassifier(MigrationConfig.from_env({}))
            db.insert_inventory_page(
                Provider.YANDEX.value,
                [
                    make_entry(Provider.YANDEX, "/docs/a.pdf"),
                    make_entry(Provider.YANDEX, "/docs/b.pdf"),
                ],
            )
            db.classify_yandex(classifier)
            db.init_sync_log(["/docs/a.pdf", "/docs/b.pdf"])
            db.record_sync_outcome("/docs/a.pdf", "synced", 1, "done")
            db.set_meta("phase", "sync")

            yandex = FakeYandexClient({})
            dropbox = FakeDropboxClient(start_result=([], None, False))
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=classifier,
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: self.fail(f"confirm_fn should not be called during sync resume: {prompt}"),
                write_reports_fn=lambda *args: None,
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(sync_runner.paths, ["/docs/b.pdf"])
            self.assertEqual(dropbox.write_checks, 1)
            self.assertEqual(yandex.read_checks, 0)
            self.assertEqual(db.get_meta("phase"), "done")
            db.close()

    def test_run_skips_yandex_inventory_when_already_done(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            db.insert_inventory_page(
                Provider.YANDEX.value,
                [make_entry(Provider.YANDEX, "/docs/a.pdf"), make_entry(Provider.YANDEX, "/docs/b.pdf")],
            )
            db.set_meta("phase", "inventory")
            db.set_meta("yandex_inventory_done", "1")

            yandex = FakeYandexClient()
            dropbox = FakeDropboxClient(start_result=([], None, False))
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(MigrationConfig.from_env({})),
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: "",
                write_reports_fn=lambda *args: None,
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(yandex.list_calls, [])
            self.assertEqual(sorted(sync_runner.paths), ["/docs/a.pdf", "/docs/b.pdf"])
            db.close()

    def test_run_respects_root_path_during_inventory_and_sync(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            config = MigrationConfig.from_env({"YD2DBX_ROOT": "/docs"})
            yandex = FakeYandexClient(
                pages={
                    0: (
                        [
                            make_entry(Provider.YANDEX, "/docs/a.pdf"),
                            make_entry(Provider.YANDEX, "/other/b.pdf"),
                        ],
                        False,
                    )
                }
            )
            dropbox = FakeDropboxClient(start_result=([], None, False))
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=config,
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(config),
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: "",
                write_reports_fn=lambda *args: None,
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(sync_runner.paths, ["/docs/a.pdf"])
            self.assertEqual(db.count_inventory(Provider.YANDEX.value), 1)
            db.close()

    def test_run_stops_after_confirmation_rejection_and_keeps_state(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            yandex = FakeYandexClient(
                pages={0: ([make_entry(Provider.YANDEX, "/docs/a.pdf")], False)}
            )
            dropbox = FakeDropboxClient(start_result=([], None, False))
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(MigrationConfig.from_env({})),
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: "n",
                write_reports_fn=lambda *args: None,
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(db.get_meta("phase"), "awaiting_confirm")
            self.assertEqual(sync_runner.paths, [])
            self.assertEqual(dropbox.write_checks, 0)
            db.close()


    def test_dropbox_inventory_parallelizes_across_top_level_folders(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            yandex = FakeYandexClient(
                pages={0: ([make_entry(Provider.YANDEX, "/docs/new.pdf")], False)}
            )
            dropbox = FakeDropboxClient(
                children_result=(
                    [make_entry(Provider.DROPBOX, "/root_file.txt", size=5)],
                    ["/Docs", "/Photos"],
                ),
                folder_start_results={
                    "/Docs": (
                        [make_entry(Provider.DROPBOX, "/Docs/old.pdf", size=20)],
                        None,
                        False,
                    ),
                    "/Photos": (
                        [make_entry(Provider.DROPBOX, "/Photos/pic.jpg", size=30)],
                        None,
                        False,
                    ),
                },
            )
            sync_runner = FakeSyncRunner()
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=FileClassifier(MigrationConfig.from_env({})),
                sync_runner=sync_runner,
                confirm_fn=lambda prompt: "",
                write_reports_fn=lambda *args: None,
            )

            result = runner.run()

            self.assertEqual(result, 0)
            self.assertEqual(db.count_inventory(Provider.DROPBOX.value), 3)
            self.assertEqual(sync_runner.paths, ["/docs/new.pdf"])
            db.close()


    def test_run_stops_immediately_on_auth_error_during_sync(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            classifier = FileClassifier(MigrationConfig.from_env({}))
            db.insert_inventory_page(
                Provider.YANDEX.value,
                [
                    make_entry(Provider.YANDEX, "/docs/a.pdf"),
                    make_entry(Provider.YANDEX, "/docs/b.pdf"),
                    make_entry(Provider.YANDEX, "/docs/c.pdf"),
                ],
            )
            db.classify_yandex(classifier)
            db.init_sync_log(["/docs/a.pdf", "/docs/b.pdf", "/docs/c.pdf"])
            db.set_meta("phase", "sync")

            class AuthFailSyncRunner:
                def transfer_one(self, candidate) -> SyncOutcome:
                    raise AuthenticationError("HTTP 401: token expired")

            yandex = FakeYandexClient({})
            dropbox = FakeDropboxClient(start_result=([], None, False))
            stage_messages: list[tuple[str, str]] = []
            runner = MigrationRunner(
                config=MigrationConfig.from_env({}),
                db=db,
                yandex=yandex,
                dropbox=dropbox,
                classifier=classifier,
                sync_runner=AuthFailSyncRunner(),
                confirm_fn=lambda prompt: self.fail("should not ask for confirmation"),
                write_reports_fn=lambda *args: None,
                print_stage_fn=lambda phase, detail: stage_messages.append((phase, detail)),
            )

            with self.assertRaises(AuthenticationError) as ctx:
                runner.run()

            self.assertIn("Токен истёк", str(ctx.exception))
            self.assertIn("Обновите токен", str(ctx.exception))
            stopped_msgs = [m for m in stage_messages if "ОСТАНОВЛЕНО" in m[1]]
            self.assertTrue(stopped_msgs, "should print ОСТАНОВЛЕНО message")
            self.assertNotEqual(db.get_meta("phase"), "done")
            db.close()


if __name__ == "__main__":
    unittest.main()
