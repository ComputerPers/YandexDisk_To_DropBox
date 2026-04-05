"""Shared path utilities used across runner, sync_runner, and CLI."""

from __future__ import annotations

from pathlib import PurePosixPath

from yd2dbx.models import InventoryEntry


def parent_folders(paths: list[str]) -> list[str]:
    """Derive unique parent folder paths, ordered by first appearance."""
    folders: list[str] = []
    seen: set[str] = set()
    for path in paths:
        current = ""
        for part in PurePosixPath(path).parts[:-1]:
            if part == "/":
                continue
            current = f"{current}/{part}"
            if current not in seen:
                seen.add(current)
                folders.append(current)
    return folders


def filter_to_root(entries: list[InventoryEntry], root_path: str) -> list[InventoryEntry]:
    """Keep only entries under the given root path prefix."""
    if root_path in {"", "/"}:
        return entries
    prefix = root_path.rstrip("/")
    return [e for e in entries if e.path == prefix or e.path.startswith(f"{prefix}/")]
