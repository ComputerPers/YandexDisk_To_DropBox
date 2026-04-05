from __future__ import annotations

from pathlib import PurePosixPath

from yd2dbx.config import MigrationConfig
from yd2dbx.models import ClassifiedEntry, InventoryEntry


DOCUMENT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
    ".md",
}
IMAGE_EXTENSIONS = {".gif", ".heic", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
ARCHIVE_EXTENSIONS = {".7z", ".apk", ".dmg", ".exe", ".iso", ".msi", ".pkg", ".rar", ".tar", ".tgz", ".zip"}
SKIP_PATH_SEGMENTS = {".venv", "venv", "__pycache__", "node_modules", ".godot", ".sync", ".mypy_cache", ".pytest_cache"}
SKIP_FILENAMES = {".DS_Store", "Thumbs.db", ".gitignore", ".gitkeep"}
GIT_REPO_SEGMENT = ".git"
DOCUMENT_MIME_TYPES = {
    "application/msword",
    "application/pdf",
    "application/rtf",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class FileClassifier:
    def __init__(self, config: MigrationConfig) -> None:
        self.config = config

    def classify(self, entry: InventoryEntry) -> ClassifiedEntry:
        path_lower = entry.path.casefold()
        path_obj = PurePosixPath(entry.path)
        suffix = path_obj.suffix.casefold()
        mime_type = (entry.mime_type or "").casefold()
        parts = {part.casefold() for part in path_obj.parts}
        filename = path_obj.name.casefold()

        if GIT_REPO_SEGMENT in parts:
            return ClassifiedEntry(entry=entry, category="git_repo", handling="separate_workflow", reason="git repository workflow")

        if any(segment in parts for segment in SKIP_PATH_SEGMENTS) or filename in {name.casefold() for name in SKIP_FILENAMES}:
            return ClassifiedEntry(entry=entry, category="dev_junk", handling="explicit_skip", reason="development cache or junk")

        if self._is_screenshot(path_lower):
            return ClassifiedEntry(entry=entry, category="screenshot", handling="explicit_skip", reason="screenshot pattern")

        if suffix in ARCHIVE_EXTENSIONS:
            reason = "archive or installer"
            if entry.size >= self.config.large_file_threshold_bytes:
                reason = "large archive or installer"
            return ClassifiedEntry(
                entry=entry,
                category="archive_or_installer",
                handling="separate_workflow",
                reason=reason,
            )

        if suffix in IMAGE_EXTENSIONS or mime_type.startswith("image/"):
            return ClassifiedEntry(entry=entry, category="image", handling="separate_workflow", reason="image workflow")

        if entry.size >= self.config.large_file_threshold_bytes:
            return ClassifiedEntry(entry=entry, category="large_file", handling="separate_workflow", reason="large file")

        if suffix in DOCUMENT_EXTENSIONS or self._is_document_mime(mime_type):
            return ClassifiedEntry(entry=entry, category="document", handling="sync", reason="document in primary migration wave")

        return ClassifiedEntry(entry=entry, category="other", handling="separate_workflow", reason="non-document follow-up workflow")

    def _is_screenshot(self, path_lower: str) -> bool:
        return any(marker in path_lower for marker in self.config.screenshot_markers)

    @staticmethod
    def _is_document_mime(mime_type: str) -> bool:
        return mime_type.startswith("text/") or mime_type in DOCUMENT_MIME_TYPES
