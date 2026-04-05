from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from yd2dbx.models import InventoryEntry, Provider, comparison_key

class SupportsClassify(Protocol):
    def classify(self, entry: InventoryEntry): ...


class MigrationDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection: sqlite3.Connection | None = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def close(self) -> None:
        if self.connection is None:
            return
        self.connection.close()
        self.connection = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO meta(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def insert_inventory_page(self, provider: str, entries: list[InventoryEntry]) -> None:
        if not entries:
            return
        rows = [
            (
                provider,
                entry.path,
                comparison_key(entry.path),
                entry.size,
                entry.modified,
                entry.mime_type,
                entry.source_hash,
                entry.source_hash_type,
            )
            for entry in entries
        ]
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO inventory(
                    provider,
                    path,
                    path_key,
                    size,
                    modified,
                    mime_type,
                    source_hash,
                    source_hash_type
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, path_key) DO UPDATE SET
                    path = excluded.path,
                    size = excluded.size,
                    modified = excluded.modified,
                    mime_type = excluded.mime_type,
                    source_hash = excluded.source_hash,
                    source_hash_type = excluded.source_hash_type
                """,
                rows,
            )

    def count_inventory(self, provider: str) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM inventory WHERE provider = ?", (provider,)).fetchone()
        return int(row["count"]) if row is not None else 0

    def classify_yandex(
        self,
        classifier: SupportsClassify,
        progress_fn: object = None,
    ) -> dict[str, int]:
        summary: Counter[str] = Counter()
        last_id = 0
        batch_size = 5000
        processed = 0

        while True:
            rows = self.connection.execute(
                """
                SELECT id, path, size, modified, mime_type, source_hash, source_hash_type
                FROM inventory
                WHERE provider = ? AND id > ?
                ORDER BY id
                LIMIT ?
                """,
                (Provider.YANDEX.value, last_id, batch_size),
            ).fetchall()
            if not rows:
                break

            updates: list[tuple[str, str, str, int]] = []
            for row in rows:
                entry = InventoryEntry(
                    provider=Provider.YANDEX,
                    path=str(row["path"]),
                    size=int(row["size"]),
                    modified=str(row["modified"]) if row["modified"] else None,
                    mime_type=str(row["mime_type"]) if row["mime_type"] else None,
                    source_hash=str(row["source_hash"]) if row["source_hash"] else None,
                    source_hash_type=str(row["source_hash_type"]) if row["source_hash_type"] else None,
                )
                classified = classifier.classify(entry)
                summary[classified.category] += 1
                updates.append((classified.category, classified.handling, classified.reason, int(row["id"])))

            with self.connection:
                self.connection.executemany(
                    "UPDATE inventory SET category = ?, handling = ?, reason = ? WHERE id = ?",
                    updates,
                )

            last_id = int(rows[-1]["id"])
            processed += len(rows)
            if callable(progress_fn):
                progress_fn(processed)

        return dict(summary)

    def known_dropbox_folders(self) -> set[str]:
        """Derive existing folder paths from Dropbox file paths in inventory."""
        rows = self.connection.execute(
            "SELECT path FROM inventory WHERE provider = ?",
            (Provider.DROPBOX.value,),
        ).fetchall()
        folders: set[str] = set()
        for row in rows:
            parts = str(row["path"]).split("/")
            for i in range(2, len(parts)):
                folders.add("/".join(parts[:i]).lower())
        return folders

    def query_sync_candidates(self) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            """
            SELECT y.path
            FROM inventory AS y
            LEFT JOIN inventory AS d
                ON d.provider = ?
               AND d.path_key = y.path_key
            WHERE y.provider = ?
              AND y.handling = 'sync'
              AND d.path_key IS NULL
            ORDER BY y.path
            """,
            (Provider.DROPBOX.value, Provider.YANDEX.value),
        ).fetchall()
        return [(str(row["path"]), str(row["path"])) for row in rows]

    def query_diff_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM (
                """ + self._diff_rows_sql() + """
            )
            GROUP BY status
            ORDER BY status
            """,
            (Provider.DROPBOX.value, Provider.YANDEX.value),
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def query_diff_items(self) -> list[dict[str, object]]:
        rows = self.connection.execute(self._diff_rows_sql(), (Provider.DROPBOX.value, Provider.YANDEX.value)).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            items.append(
                {
                    "status": str(row["status"]),
                    "reason": str(row["decision_reason"]),
                    "entry": {
                        "provider": Provider.YANDEX.value,
                        "path": str(row["path"]),
                        "size": int(row["size"]),
                        "modified": str(row["modified"]) if row["modified"] else None,
                        "mime_type": str(row["mime_type"]) if row["mime_type"] else None,
                        "source_hash": str(row["source_hash"]) if row["source_hash"] else None,
                        "source_hash_type": str(row["source_hash_type"]) if row["source_hash_type"] else None,
                        "category": str(row["category"]) if row["category"] else None,
                        "handling": str(row["handling"]) if row["handling"] else None,
                        "reason": str(row["entry_reason"]) if row["entry_reason"] else None,
                    },
                }
            )
        return items

    def get_inventory_entry(self, provider: str, path: str) -> InventoryEntry | None:
        row = self.connection.execute(
            """
            SELECT path, size, modified, mime_type, source_hash, source_hash_type
            FROM inventory
            WHERE provider = ? AND path = ?
            """,
            (provider, path),
        ).fetchone()
        if row is None:
            return None
        return InventoryEntry(
            provider=Provider(provider),
            path=str(row["path"]),
            size=int(row["size"]),
            modified=str(row["modified"]) if row["modified"] else None,
            mime_type=str(row["mime_type"]) if row["mime_type"] else None,
            source_hash=str(row["source_hash"]) if row["source_hash"] else None,
            source_hash_type=str(row["source_hash_type"]) if row["source_hash_type"] else None,
        )

    def init_sync_log(self, paths: list[str]) -> None:
        if not paths:
            return
        timestamp = _utc_now()
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO sync_log(path, status, attempts, detail, updated_at)
                VALUES(?, 'pending', 0, '', ?)
                """,
                [(path, timestamp) for path in paths],
            )

    def pending_sync_paths(self) -> list[str]:
        rows = self.connection.execute(
            "SELECT path FROM sync_log WHERE status = 'pending' ORDER BY path"
        ).fetchall()
        return [str(row["path"]) for row in rows]

    def pending_sync_entries(self) -> list[tuple[InventoryEntry, str, str, str]]:
        """One query: pending paths joined with Yandex inventory + classification."""
        rows = self.connection.execute(
            """
            SELECT i.path, i.size, i.modified, i.mime_type,
                   i.source_hash, i.source_hash_type,
                   i.category, i.handling, i.reason
            FROM sync_log s
            JOIN inventory i ON i.path = s.path AND i.provider = ?
            WHERE s.status = 'pending'
            ORDER BY s.path
            """,
            (Provider.YANDEX.value,),
        ).fetchall()
        return [
            (
                InventoryEntry(
                    provider=Provider.YANDEX,
                    path=str(row["path"]),
                    size=int(row["size"]),
                    modified=str(row["modified"]) if row["modified"] else None,
                    mime_type=str(row["mime_type"]) if row["mime_type"] else None,
                    source_hash=str(row["source_hash"]) if row["source_hash"] else None,
                    source_hash_type=str(row["source_hash_type"]) if row["source_hash_type"] else None,
                ),
                str(row["category"] or "document"),
                str(row["handling"] or "sync"),
                str(row["reason"] or ""),
            )
            for row in rows
        ]

    def record_sync_outcome(self, path: str, status: str, attempts: int, detail: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO sync_log(path, status, attempts, detail, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    status = excluded.status,
                    attempts = excluded.attempts,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (path, status, attempts, detail, _utc_now()),
            )

    def sync_summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM sync_log GROUP BY status ORDER BY status"
        ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def list_sync_outcomes(self) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT path, status, attempts, detail FROM sync_log ORDER BY path"
        ).fetchall()
        return [
            {
                "path": str(row["path"]),
                "status": str(row["status"]),
                "attempts": int(row["attempts"]),
                "detail": str(row["detail"]) if row["detail"] else "",
            }
            for row in rows
        ]

    def _initialize(self) -> None:
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    provider TEXT NOT NULL,
                    path TEXT NOT NULL,
                    path_key TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    modified TEXT,
                    mime_type TEXT,
                    source_hash TEXT,
                    source_hash_type TEXT,
                    category TEXT,
                    handling TEXT,
                    reason TEXT,
                    UNIQUE(provider, path_key)
                );
                CREATE INDEX IF NOT EXISTS idx_inv_provider_key ON inventory(provider, path_key);
                CREATE INDEX IF NOT EXISTS idx_inv_handling ON inventory(provider, handling);

                CREATE TABLE IF NOT EXISTS sync_log (
                    path TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER DEFAULT 0,
                    detail TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )

    @staticmethod
    def _diff_rows_sql() -> str:
        return """
            SELECT
                y.path,
                y.size,
                y.modified,
                y.mime_type,
                y.source_hash,
                y.source_hash_type,
                y.category,
                y.handling,
                y.reason AS entry_reason,
                CASE
                    WHEN y.handling = 'explicit_skip' THEN 'explicit_skip'
                    WHEN y.handling != 'sync' THEN 'unsupported_for_first_pass'
                    WHEN d.path_key IS NULL THEN 'missing_in_dropbox'
                    WHEN y.size = d.size
                     AND COALESCE(y.modified, '') = COALESCE(d.modified, '') THEN 'exact_metadata_match_candidate'
                    ELSE 'path_exists_but_differs'
                END AS status,
                CASE
                    WHEN y.handling = 'explicit_skip' THEN COALESCE(y.reason, '')
                    WHEN y.handling != 'sync' THEN COALESCE(y.reason, '')
                    WHEN d.path_key IS NULL THEN 'path absent in Dropbox'
                    WHEN y.size = d.size
                     AND COALESCE(y.modified, '') = COALESCE(d.modified, '') THEN 'same path, size and modified timestamp'
                    ELSE 'path exists in Dropbox but metadata differs'
                END AS decision_reason
            FROM inventory AS y
            LEFT JOIN inventory AS d
                ON d.provider = ?
               AND d.path_key = y.path_key
            WHERE y.provider = ?
            ORDER BY y.path
        """


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
