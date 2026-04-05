import unittest

from yd2dbx.classifier import FileClassifier
from yd2dbx.config import MigrationConfig
from yd2dbx.diff_engine import DiffEngine
from yd2dbx.models import InventoryEntry, Provider


def yandex_entry(path: str, size: int, modified: str, mime_type: str) -> InventoryEntry:
    return InventoryEntry(
        provider=Provider.YANDEX,
        path=path,
        size=size,
        modified=modified,
        mime_type=mime_type,
        source_hash="yd-md5",
        source_hash_type="md5",
    )


def dropbox_entry(path: str, size: int, modified: str) -> InventoryEntry:
    return InventoryEntry(
        provider=Provider.DROPBOX,
        path=path,
        size=size,
        modified=modified,
        mime_type="application/pdf",
        source_hash="dbx-hash",
        source_hash_type="content_hash",
    )


class DiffEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = FileClassifier(MigrationConfig.from_env({}))
        self.engine = DiffEngine()

    def test_marks_missing_documents_for_sync(self) -> None:
        plan = self.engine.build_plan(
            [self.classifier.classify(yandex_entry("/docs/plan.pdf", 120, "2024-01-01T10:00:00+00:00", "application/pdf"))],
            [],
        )

        self.assertEqual(plan.items[0].status, "missing_in_dropbox")
        self.assertEqual(plan.sync_candidates[0].entry.path, "/docs/plan.pdf")

    def test_marks_probable_metadata_match_separately(self) -> None:
        plan = self.engine.build_plan(
            [self.classifier.classify(yandex_entry("/docs/plan.pdf", 120, "2024-01-01T10:00:00+00:00", "application/pdf"))],
            [dropbox_entry("/docs/plan.pdf", 120, "2024-01-01T10:00:00+00:00")],
        )

        self.assertEqual(plan.items[0].status, "exact_metadata_match_candidate")
        self.assertEqual(plan.summary["exact_metadata_match_candidate"], 1)

    def test_marks_path_conflicts_for_review(self) -> None:
        plan = self.engine.build_plan(
            [self.classifier.classify(yandex_entry("/docs/plan.pdf", 120, "2024-01-01T10:00:00+00:00", "application/pdf"))],
            [dropbox_entry("/docs/plan.pdf", 999, "2024-01-01T10:00:00+00:00")],
        )

        self.assertEqual(plan.items[0].status, "path_exists_but_differs")

    def test_respects_skip_and_separate_workflow_categories(self) -> None:
        screenshot = self.classifier.classify(
            yandex_entry("/Screenshots/Screen Shot 2024-05-01 at 00.00.00.png", 120, "2024-01-01T10:00:00+00:00", "image/png")
        )
        image = self.classifier.classify(
            yandex_entry("/Photos/IMG_0001.jpg", 120, "2024-01-01T10:00:00+00:00", "image/jpeg")
        )

        plan = self.engine.build_plan([screenshot, image], [])
        statuses = [item.status for item in plan.items]

        self.assertEqual(statuses, ["explicit_skip", "unsupported_for_first_pass"])


if __name__ == "__main__":
    unittest.main()
