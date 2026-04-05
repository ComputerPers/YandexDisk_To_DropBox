import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from yd2dbx.cli import build_parser, main
from yd2dbx.reporting import render_markdown_summary


class ReportingAndCliTests(unittest.TestCase):
    def test_markdown_summary_lists_key_buckets(self) -> None:
        markdown = render_markdown_summary(
            {
                "summary": {
                    "missing_in_dropbox": 2,
                    "exact_metadata_match_candidate": 3,
                    "path_exists_but_differs": 1,
                    "unsupported_for_first_pass": 4,
                    "explicit_skip": 5,
                },
                "diff_items": [
                    {"status": "missing_in_dropbox", "entry": {"path": "/docs/a.pdf"}, "reason": "missing"},
                    {"status": "explicit_skip", "entry": {"path": "/screens/shot.png"}, "reason": "screenshot"},
                ],
                "sync_outcomes": [
                    {"status": "synced", "path": "/docs/a.pdf", "detail": "server-side transfer completed"},
                ],
            }
        )

        self.assertIn("missing_in_dropbox", markdown)
        self.assertIn("explicit_skip", markdown)
        self.assertIn("/docs/a.pdf", markdown)
        self.assertIn("synced", markdown)

    def test_cli_sync_requires_explicit_execute_flag_for_writes(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["sync"])
        execute_args = parser.parse_args(["sync", "--execute"])

        self.assertFalse(args.execute)
        self.assertTrue(execute_args.execute)

    def test_cli_accepts_saved_inventory_report_for_diff_and_sync(self) -> None:
        parser = build_parser()

        diff_args = parser.parse_args(["diff", "--inventory-json", "reports/inventory.json"])
        sync_args = parser.parse_args(["sync", "--inventory-json", "reports/inventory.json"])
        run_args = parser.parse_args(["run", "--db", "state.db", "--reset"])

        self.assertEqual(diff_args.inventory_json, "reports/inventory.json")
        self.assertEqual(sync_args.inventory_json, "reports/inventory.json")
        self.assertEqual(run_args.db, "state.db")
        self.assertTrue(run_args.reset)

    def test_cli_inventory_runs_access_checks_before_inventory(self) -> None:
        events = []

        class FakeYandexClient:
            def __init__(self, token):
                self.token = token

            def check_read_access(self):
                events.append("yandex-check")

            def list_all_files(self):
                events.append("yandex-list")
                return []

        class FakeDropboxClient:
            def __init__(self, token, **kwargs):
                self.token = token

            def check_read_access(self):
                events.append("dropbox-read-check")

            def list_all_files(self, root):
                events.append(("dropbox-list", root))
                return []

        with patch("yd2dbx.cli.YandexDiskClient", FakeYandexClient), patch(
            "yd2dbx.cli.DropboxClient", FakeDropboxClient
        ), patch("yd2dbx.cli.write_reports", lambda *args, **kwargs: None), patch(
            "yd2dbx.cli.MigrationConfig.from_env",
            return_value=type(
                "Config",
                (),
                {"yandex_token": "yd", "dropbox_token": "db", "root_path": "/", "report_dir": "reports",
                 "dropbox_refresh_token": "", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_token_file": ""},
            )(),
        ):
            result = main(["inventory"])

        self.assertEqual(result, 0)
        self.assertEqual(events, ["yandex-check", "dropbox-read-check", "yandex-list", ("dropbox-list", "")])

    def test_cli_sync_execute_runs_write_access_check(self) -> None:
        events = []

        class FakeYandexClient:
            def __init__(self, token):
                self.token = token

            def check_read_access(self):
                events.append("yandex-check")

            def list_all_files(self):
                events.append("yandex-list")
                return []

        class FakeDropboxClient:
            def __init__(self, token, **kwargs):
                self.token = token

            def check_read_access(self):
                events.append("dropbox-read-check")

            def check_write_access(self):
                events.append("dropbox-write-check")

            def list_all_files(self, root):
                events.append(("dropbox-list", root))
                return []

        class FakeRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self, sync_candidates):
                events.append(("runner", len(sync_candidates)))
                return []

        with patch("yd2dbx.cli.YandexDiskClient", FakeYandexClient), patch(
            "yd2dbx.cli.DropboxClient", FakeDropboxClient
        ), patch("yd2dbx.cli.SyncRunner", FakeRunner), patch(
            "yd2dbx.cli.write_reports", lambda *args, **kwargs: None
        ), patch(
            "yd2dbx.cli.MigrationConfig.from_env",
            return_value=type(
                "Config",
                (),
                {"yandex_token": "yd", "dropbox_token": "db", "root_path": "/", "report_dir": "reports",
                 "dropbox_refresh_token": "", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_token_file": ""},
            )(),
        ):
            result = main(["sync", "--execute"])

        self.assertEqual(result, 0)
        self.assertIn("dropbox-write-check", events)

    def test_cli_run_invokes_migration_runner(self) -> None:
        events = []

        class FakeRunner:
            def __init__(self, **kwargs):
                events.append(("runner-init", sorted(kwargs.keys())))

            def run(self):
                events.append("runner-run")
                return 0

        class FakeDB:
            def __init__(self, path):
                self.path = path
                events.append(("db-init", path))

            def close(self):
                events.append("db-close")

        with patch("yd2dbx.cli.YandexDiskClient", lambda token: ("yandex", token)), patch(
            "yd2dbx.cli.DropboxClient", lambda token, **kw: ("dropbox", token)
        ), patch("yd2dbx.cli.SyncRunner", lambda **kwargs: ("sync-runner", kwargs)), patch(
            "yd2dbx.cli.MigrationRunner", FakeRunner
        ), patch("yd2dbx.cli.MigrationDB", FakeDB), patch(
            "yd2dbx.cli.MigrationConfig.from_env",
            return_value=type(
                "Config",
                (),
                {"yandex_token": "yd", "dropbox_token": "db", "root_path": "/", "report_dir": "reports",
                 "dropbox_refresh_token": "", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_token_file": ""},
            )(),
        ):
            result = main(["run", "--db", "state.db"])

        self.assertEqual(result, 0)
        self.assertIn(("db-init", "state.db"), events)
        self.assertIn("runner-run", events)
        self.assertIn("db-close", events)

    def test_cli_run_reset_removes_existing_db_before_runner_starts(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "state.db"
            db_path.write_text("old")
            events = []

            class FakeRunner:
                def __init__(self, **kwargs):
                    events.append("runner-init")

                def run(self):
                    events.append("runner-run")
                    return 0

            class FakeDB:
                def __init__(self, path):
                    events.append(("exists-before-db-init", Path(path).exists()))

                def close(self):
                    events.append("db-close")

            with patch("yd2dbx.cli.YandexDiskClient", lambda token: ("yandex", token)), patch(
                "yd2dbx.cli.DropboxClient", lambda token, **kw: ("dropbox", token)
            ), patch("yd2dbx.cli.SyncRunner", lambda **kwargs: ("sync-runner", kwargs)), patch(
                "yd2dbx.cli.MigrationRunner", FakeRunner
            ), patch("yd2dbx.cli.MigrationDB", FakeDB), patch(
                "yd2dbx.cli.MigrationConfig.from_env",
                return_value=type(
                    "Config",
                    (),
                    {"yandex_token": "yd", "dropbox_token": "db", "root_path": "/", "report_dir": "reports",
                     "dropbox_refresh_token": "", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_token_file": ""},
                )(),
            ):
                result = main(["run", "--db", str(db_path), "--reset"])

        self.assertEqual(result, 0)
        self.assertIn(("exists-before-db-init", False), events)

    def test_cli_run_prints_readable_error_for_runtime_failures(self) -> None:
        stderr = io.StringIO()

        class FakeRunner:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def run(self):
                raise RuntimeError("Request to https://cloud-api.yandex.net/v1/disk timed out after 30s")

        class FakeDB:
            def __init__(self, path):
                self.path = path

            def close(self):
                pass

        with patch("yd2dbx.cli.YandexDiskClient", lambda token: ("yandex", token)), patch(
            "yd2dbx.cli.DropboxClient", lambda token, **kw: ("dropbox", token)
        ), patch("yd2dbx.cli.SyncRunner", lambda **kwargs: ("sync-runner", kwargs)), patch(
            "yd2dbx.cli.MigrationRunner", FakeRunner
        ), patch("yd2dbx.cli.MigrationDB", FakeDB), patch(
            "yd2dbx.cli.MigrationConfig.from_env",
            return_value=type(
                "Config",
                (),
                {"yandex_token": "yd", "dropbox_token": "db", "root_path": "/", "report_dir": "reports",
                 "dropbox_refresh_token": "", "dropbox_app_key": "", "dropbox_app_secret": "", "dropbox_token_file": ""},
            )(),
        ), patch("sys.stderr", stderr):
            result = main(["run", "--db", "state.db"])

        self.assertEqual(result, 1)
        self.assertIn("timed out", stderr.getvalue().lower())
        self.assertNotIn("traceback", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
