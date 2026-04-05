import unittest

from yd2dbx.classifier import FileClassifier
from yd2dbx.config import MigrationConfig
from yd2dbx.diff_engine import DiffEngine
from yd2dbx.models import InventoryEntry, Provider
from yd2dbx.sync_runner import SyncRunner
from yd2dbx.transport import AuthenticationError


class FakeYandexClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_download_url(self, path: str) -> str:
        self.calls.append(path)
        return f"https://download.example{path}"

    def download_file(self, path: str) -> bytes:
        return b"fake file content"


class FakeDropboxClient:
    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.parents: list[str] = []
        self.uploaded: list[tuple[str, bytes]] = []
        self.job_attempts = 0

    def ensure_folders(self, folders: list[str]) -> None:
        self.parents.extend(folders)

    def save_url(self, path: str, url: str) -> str:
        self.saved.append((path, url))
        self.job_attempts += 1
        if self.job_attempts == 1:
            raise RuntimeError("temporary Dropbox error")
        return "job-2"

    def check_save_url_job(self, job_id: str):
        return type("SaveStatus", (), {"tag": "complete", "metadata": {"job_id": job_id}})()

    def upload_small_file(self, path: str, data: bytes) -> None:
        self.uploaded.append((path, data))


class FakeDropboxPendingClient(FakeDropboxClient):
    def save_url(self, path: str, url: str) -> str:
        self.saved.append((path, url))
        return "job-pending"

    def check_save_url_job(self, job_id: str):
        return type("SaveStatus", (), {"tag": "in_progress", "metadata": {"job_id": job_id}})()


class SyncRunnerTests(unittest.TestCase):
    def test_transfer_one_is_available_for_incremental_runner_usage(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "2"})
        classifier = FileClassifier(config)
        engine = DiffEngine()
        yandex = FakeYandexClient()
        dropbox = FakeDropboxClient()

        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/project/one.pdf",
            size=42,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-1",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)
        result = runner.transfer_one(plan.sync_candidates[0])

        self.assertEqual(result.status, "synced")
        self.assertEqual(result.path, "/docs/project/one.pdf")

    def test_retries_server_side_transfer_and_creates_parent_folders(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "2"})
        classifier = FileClassifier(config)
        engine = DiffEngine()
        yandex = FakeYandexClient()
        dropbox = FakeDropboxClient()

        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/project/plan.pdf",
            size=42,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-1",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)
        results = runner.run(plan.sync_candidates)

        self.assertEqual(results[0].status, "synced")
        self.assertEqual(dropbox.parents, ["/docs", "/docs/project"])
        self.assertEqual(len(dropbox.saved), 2)
        self.assertEqual(yandex.calls, ["/docs/project/plan.pdf", "/docs/project/plan.pdf"])

    def test_marks_review_required_when_job_never_finishes(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "1", "YD2DBX_MAX_POLLS": "2"})
        classifier = FileClassifier(config)
        engine = DiffEngine()
        yandex = FakeYandexClient()
        dropbox = FakeDropboxPendingClient()

        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/project/stuck.pdf",
            size=42,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-1",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)
        results = runner.run(plan.sync_candidates)

        self.assertEqual(results[0].status, "synced")
        self.assertIn("fallback", results[0].detail)
        self.assertEqual(len(dropbox.uploaded), 1)
        self.assertEqual(dropbox.uploaded[0][0], "/docs/project/stuck.pdf")


    def test_auth_error_propagates_immediately_from_transfer_one(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "2"})
        classifier = FileClassifier(config)
        engine = DiffEngine()

        class AuthFailYandex:
            def get_download_url(self, path: str) -> str:
                raise AuthenticationError("HTTP 401: token expired")

            def download_file(self, path: str) -> bytes:
                raise AuthenticationError("HTTP 401: token expired")

        yandex = AuthFailYandex()
        dropbox = FakeDropboxClient()

        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/secret.pdf",
            size=42,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-1",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)

        with self.assertRaises(AuthenticationError):
            runner.transfer_one(plan.sync_candidates[0])

    def test_auth_error_from_dropbox_fallback_propagates(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "1", "YD2DBX_MAX_POLLS": "1"})
        classifier = FileClassifier(config)
        engine = DiffEngine()

        class AuthFailDropbox(FakeDropboxPendingClient):
            def upload_small_file(self, path: str, data: bytes) -> None:
                raise AuthenticationError("HTTP 401: expired")

        yandex = FakeYandexClient()
        dropbox = AuthFailDropbox()

        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/file.pdf",
            size=42,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-1",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)

        with self.assertRaises(AuthenticationError):
            runner.transfer_one(plan.sync_candidates[0])


    def test_fallback_rejects_file_exceeding_upload_limit(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_MAX_RETRIES": "1", "YD2DBX_MAX_POLLS": "1"})
        classifier = FileClassifier(config)
        engine = DiffEngine()
        yandex = FakeYandexClient()
        dropbox = FakeDropboxPendingClient()

        big_size = 160 * 1024 * 1024
        entry = InventoryEntry(
            provider=Provider.YANDEX,
            path="/docs/huge.pdf",
            size=big_size,
            modified="2024-01-01T00:00:00+00:00",
            mime_type="application/pdf",
            source_hash="md5-big",
            source_hash_type="md5",
        )
        plan = engine.build_plan([classifier.classify(entry)], [])

        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox, sleep_func=lambda _: None)
        result = runner.transfer_one(plan.sync_candidates[0])

        self.assertEqual(result.status, "review_required")
        self.assertIn("150MB", result.detail)
        self.assertEqual(dropbox.uploaded, [])


if __name__ == "__main__":
    unittest.main()
