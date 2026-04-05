import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from yd2dbx.classifier import FileClassifier
from yd2dbx.config import MigrationConfig
from yd2dbx.db import MigrationDB
from yd2dbx.models import ClassifiedEntry, InventoryEntry, Provider


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


class MigrationDBTests(unittest.TestCase):
    def test_insert_inventory_pages_and_persist_meta_across_reopen(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "state.db"
            db = MigrationDB(db_path)

            db.insert_inventory_page(
                Provider.YANDEX.value,
                [
                    make_entry(Provider.YANDEX, "/docs/a.pdf"),
                    make_entry(Provider.YANDEX, "/docs/b.pdf"),
                ],
            )
            db.set_meta("phase", "yandex_inventory")
            db.close()

            reopened = MigrationDB(db_path)
            self.assertEqual(reopened.count_inventory(Provider.YANDEX.value), 2)
            self.assertEqual(reopened.get_meta("phase"), "yandex_inventory")
            reopened.close()

    def test_classify_yandex_updates_inventory_rows(self) -> None:
        class FakeClassifier:
            def classify(self, entry: InventoryEntry) -> ClassifiedEntry:
                if ".git/" in entry.path:
                    return ClassifiedEntry(entry=entry, category="git_repo", handling="separate_workflow", reason="git repo")
                if ".venv/" in entry.path:
                    return ClassifiedEntry(entry=entry, category="dev_junk", handling="explicit_skip", reason="virtualenv")
                if "Screenshots" in entry.path:
                    return ClassifiedEntry(entry=entry, category="screenshot", handling="explicit_skip", reason="screenshot")
                return ClassifiedEntry(entry=entry, category="document", handling="sync", reason="document")

        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            db.insert_inventory_page(
                Provider.YANDEX.value,
                [
                    make_entry(Provider.YANDEX, "/docs/a.pdf", mime_type="application/pdf"),
                    make_entry(Provider.YANDEX, "/Screenshots/shot.png", mime_type="image/png"),
                    make_entry(Provider.YANDEX, "/repo/.git/objects/aa/blob", mime_type="application/octet-stream"),
                    make_entry(Provider.YANDEX, "/repo/.venv/lib/site.py", mime_type="text/x-python"),
                ],
            )

            summary = db.classify_yandex(FakeClassifier())

            self.assertEqual(summary["document"], 1)
            self.assertEqual(summary["screenshot"], 1)
            self.assertEqual(summary["git_repo"], 1)
            self.assertEqual(summary["dev_junk"], 1)

            rows = self._fetch_rows(db.path, "SELECT path, category, handling FROM inventory ORDER BY path")
            self.assertEqual(
                rows,
                [
                    ("/Screenshots/shot.png", "screenshot", "explicit_skip"),
                    ("/docs/a.pdf", "document", "sync"),
                    ("/repo/.git/objects/aa/blob", "git_repo", "separate_workflow"),
                    ("/repo/.venv/lib/site.py", "dev_junk", "explicit_skip"),
                ],
            )
            db.close()

    def test_query_sync_candidates_and_diff_summary_use_sql_join(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            classifier = FileClassifier(MigrationConfig.from_env({}))

            db.insert_inventory_page(
                Provider.YANDEX.value,
                [
                    make_entry(Provider.YANDEX, "/docs/match.pdf", size=10, modified="2024-01-01T00:00:00+00:00"),
                    make_entry(Provider.YANDEX, "/docs/diff.pdf", size=20, modified="2024-01-01T00:00:00+00:00"),
                    make_entry(Provider.YANDEX, "/docs/missing.pdf", size=30, modified="2024-01-01T00:00:00+00:00"),
                    make_entry(Provider.YANDEX, "/images/a.jpg", size=40, mime_type="image/jpeg"),
                    make_entry(Provider.YANDEX, "/Screenshots/shot.png", size=50, mime_type="image/png"),
                ],
            )
            db.insert_inventory_page(
                Provider.DROPBOX.value,
                [
                    make_entry(Provider.DROPBOX, "/docs/match.pdf", size=10, modified="2024-01-01T00:00:00+00:00"),
                    make_entry(Provider.DROPBOX, "/docs/diff.pdf", size=999, modified="2024-01-02T00:00:00+00:00"),
                ],
            )
            db.classify_yandex(classifier)

            candidates = db.query_sync_candidates()
            summary = db.query_diff_summary()

            self.assertEqual(candidates, [("/docs/missing.pdf", "/docs/missing.pdf")])
            self.assertEqual(
                summary,
                {
                    "exact_metadata_match_candidate": 1,
                    "explicit_skip": 1,
                    "missing_in_dropbox": 1,
                    "path_exists_but_differs": 1,
                    "unsupported_for_first_pass": 1,
                },
            )
            db.close()

    def test_sync_log_tracks_pending_and_completed_paths(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")

            db.init_sync_log(["/docs/a.pdf", "/docs/b.pdf"])
            self.assertEqual(db.pending_sync_paths(), ["/docs/a.pdf", "/docs/b.pdf"])

            db.record_sync_outcome("/docs/a.pdf", "synced", 2, "server-side transfer completed")

            self.assertEqual(db.pending_sync_paths(), ["/docs/b.pdf"])
            self.assertEqual(db.sync_summary(), {"pending": 1, "synced": 1})
            db.close()

    def test_known_dropbox_folders(self) -> None:
        with TemporaryDirectory() as tmp:
            db = MigrationDB(Path(tmp) / "state.db")
            db.insert_inventory_page(
                Provider.DROPBOX,
                [
                    make_entry(Provider.DROPBOX, "/alpha/beta/file.txt"),
                    make_entry(Provider.DROPBOX, "/alpha/gamma/deep/doc.pdf"),
                    make_entry(Provider.DROPBOX, "/root_file.txt"),
                ],
            )
            folders = db.known_dropbox_folders()
            self.assertEqual(
                folders,
                {"/alpha", "/alpha/beta", "/alpha/gamma", "/alpha/gamma/deep"},
            )
            db.close()

    def test_wal_journal_mode_is_enabled(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            db = MigrationDB(Path(tmp_dir) / "state.db")
            row = db.connection.execute("PRAGMA journal_mode").fetchone()
            self.assertEqual(row[0], "wal")
            db.close()

    @staticmethod
    def _fetch_rows(db_path: Path, query: str) -> list[tuple[str, str, str]]:
        connection = sqlite3.connect(db_path)
        try:
            cursor = connection.execute(query)
            return list(cursor.fetchall())
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
