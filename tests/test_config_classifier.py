import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from yd2dbx.classifier import FileClassifier
from yd2dbx.config import MigrationConfig
from yd2dbx.models import InventoryEntry, Provider


def make_entry(path: str, size: int = 128, mime_type: str | None = None) -> InventoryEntry:
    return InventoryEntry(
        provider=Provider.YANDEX,
        path=path,
        size=size,
        modified="2024-01-01T00:00:00+00:00",
        mime_type=mime_type,
        source_hash="hash",
        source_hash_type="md5",
    )


class ConfigAndClassifierTests(unittest.TestCase):
    def test_load_config_from_environment_with_safe_defaults(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            config = MigrationConfig.from_env(
                {
                    "YANDEX_DISK_TOKEN": "yd-token",
                    "DROPBOX_TOKEN": "dbx-token",
                    "YD2DBX_ROOT": "/Docs",
                    "YD2DBX_REPORT_DIR": "reports/custom",
                },
                base_dir=Path(tmp_dir),
            )

        self.assertEqual(config.yandex_token, "yd-token")
        self.assertEqual(config.dropbox_token, "dbx-token")
        self.assertEqual(config.root_path, "/Docs")
        self.assertEqual(config.report_dir, "reports/custom")
        self.assertTrue(config.dry_run)
        self.assertGreater(config.large_file_threshold_bytes, 0)

    def test_load_config_uses_local_token_files_when_env_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".yadisk").write_text("yd-from-file\n")
            (root / ".dropbox").write_text("dbx-from-file\n")

            config = MigrationConfig.from_env({}, base_dir=root)

        self.assertEqual(config.yandex_token, "yd-from-file")
        self.assertEqual(config.dropbox_token, "dbx-from-file")

    def test_load_config_prefers_local_token_files_over_environment(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".yadisk").write_text("yd-from-file\n")
            (root / ".dropbox").write_text("dbx-from-file\n")

            config = MigrationConfig.from_env(
                {
                    "YANDEX_DISK_TOKEN": "yd-from-env",
                    "DROPBOX_TOKEN": "dbx-from-env",
                },
                base_dir=root,
            )

        self.assertEqual(config.yandex_token, "yd-from-file")
        self.assertEqual(config.dropbox_token, "dbx-from-file")

    def test_load_config_parses_dropbox_refresh_token_format(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".dropbox").write_text(
                "app_key=mykey\n"
                "app_secret=mysecret\n"
                "refresh_token=myrefresh\n"
            )

            config = MigrationConfig.from_env({}, base_dir=root)

        self.assertEqual(config.dropbox_app_key, "mykey")
        self.assertEqual(config.dropbox_app_secret, "mysecret")
        self.assertEqual(config.dropbox_refresh_token, "myrefresh")
        self.assertEqual(config.dropbox_token, "")
        self.assertTrue(config.dropbox_token_file.endswith(".dropbox"))

    def test_load_config_parses_dropbox_refresh_with_cached_access_token(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            (root / ".dropbox").write_text(
                "app_key=mykey\n"
                "app_secret=mysecret\n"
                "refresh_token=myrefresh\n"
                "access_token=cached_tok\n"
            )

            config = MigrationConfig.from_env({}, base_dir=root)

        self.assertEqual(config.dropbox_refresh_token, "myrefresh")
        self.assertEqual(config.dropbox_token, "cached_tok")

    def test_classifier_marks_documents_for_primary_sync(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        classified = classifier.classify(make_entry("/docs/specification.pdf", mime_type="application/pdf"))

        self.assertEqual(classified.category, "document")
        self.assertEqual(classified.handling, "sync")

    def test_classifier_skips_screenshots_explicitly(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        classified = classifier.classify(make_entry("/Screenshots/Screen Shot 2025-03-03 at 10.12.12.png"))

        self.assertEqual(classified.category, "screenshot")
        self.assertEqual(classified.handling, "explicit_skip")

    def test_classifier_routes_images_to_separate_workflow(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        classified = classifier.classify(make_entry("/Photos/IMG_0012.JPG", mime_type="image/jpeg"))

        self.assertEqual(classified.category, "image")
        self.assertEqual(classified.handling, "separate_workflow")

    def test_classifier_routes_large_archives_to_separate_workflow(self) -> None:
        config = MigrationConfig.from_env({"YD2DBX_LARGE_FILE_THRESHOLD_MB": "1"})
        classifier = FileClassifier(config)

        classified = classifier.classify(make_entry("/dist/toolkit.zip", size=5 * 1024 * 1024))

        self.assertEqual(classified.category, "archive_or_installer")
        self.assertEqual(classified.handling, "separate_workflow")
        self.assertIn("large", classified.reason)

    def test_classifier_does_not_treat_octet_stream_as_document(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        classified = classifier.classify(make_entry("/downloads/blob.bin", mime_type="application/octet-stream"))

        self.assertNotEqual(classified.handling, "sync")

    def test_classifier_routes_git_directories_to_separate_workflow(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        classified = classifier.classify(make_entry("/repo/.git/objects/ab/cdef", mime_type="application/octet-stream"))

        self.assertEqual(classified.category, "git_repo")
        self.assertEqual(classified.handling, "separate_workflow")

    def test_classifier_skips_virtualenv_and_pycache_as_dev_junk(self) -> None:
        classifier = FileClassifier(MigrationConfig.from_env({}))

        venv_classified = classifier.classify(make_entry("/repo/.venv/lib/site.py", mime_type="text/x-python"))
        pycache_classified = classifier.classify(make_entry("/repo/__pycache__/module.pyc", mime_type="application/octet-stream"))

        self.assertEqual(venv_classified.category, "dev_junk")
        self.assertEqual(venv_classified.handling, "explicit_skip")
        self.assertEqual(pycache_classified.category, "dev_junk")
        self.assertEqual(pycache_classified.handling, "explicit_skip")


if __name__ == "__main__":
    unittest.main()
