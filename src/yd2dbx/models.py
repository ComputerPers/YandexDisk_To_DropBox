from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


class Provider(StrEnum):
    YANDEX = "yandex"
    DROPBOX = "dropbox"


def normalize_path(path: str) -> str:
    normalized = path.strip()
    if normalized.startswith("disk:"):
        normalized = normalized[5:]
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized or "/"


def comparison_key(path: str) -> str:
    return normalize_path(path).casefold()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    candidate = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(slots=True)
class InventoryEntry:
    provider: Provider
    path: str
    size: int
    modified: str | None
    mime_type: str | None
    source_hash: str | None
    source_hash_type: str | None

    def __post_init__(self) -> None:
        self.path = normalize_path(self.path)

    @property
    def key(self) -> str:
        return comparison_key(self.path)

    @property
    def modified_dt(self) -> datetime | None:
        return parse_datetime(self.modified)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ClassifiedEntry:
    entry: InventoryEntry
    category: str
    handling: str
    reason: str

    @property
    def path(self) -> str:
        return self.entry.path

    def to_dict(self) -> dict[str, object]:
        data = {
            "category": self.category,
            "handling": self.handling,
            "reason": self.reason,
        }
        data.update(self.entry.to_dict())
        return data


@dataclass(slots=True)
class DiffDecision:
    entry: ClassifiedEntry
    status: str
    reason: str
    matching_dropbox_entry: InventoryEntry | None = None

    def to_dict(self) -> dict[str, object]:
        data = {
            "status": self.status,
            "reason": self.reason,
            "entry": self.entry.to_dict(),
        }
        if self.matching_dropbox_entry is not None:
            data["matching_dropbox_entry"] = self.matching_dropbox_entry.to_dict()
        return data


@dataclass(slots=True)
class DiffPlan:
    items: list[DiffDecision]
    summary: dict[str, int]
    sync_candidates: list[DiffDecision]

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": dict(self.summary),
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(slots=True)
class SaveUrlJobStatus:
    tag: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class SyncOutcome:
    path: str
    status: str
    attempts: int
    detail: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
