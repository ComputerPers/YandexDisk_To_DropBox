from __future__ import annotations

from collections import Counter

from yd2dbx.models import ClassifiedEntry, DiffDecision, DiffPlan, InventoryEntry


class DiffEngine:
    def build_plan(self, yandex_entries: list[ClassifiedEntry], dropbox_entries: list[InventoryEntry]) -> DiffPlan:
        dropbox_by_key = {entry.key: entry for entry in dropbox_entries}
        decisions: list[DiffDecision] = []
        sync_candidates: list[DiffDecision] = []

        for classified in yandex_entries:
            decision = self._classify_decision(classified, dropbox_by_key.get(classified.entry.key))
            decisions.append(decision)
            if decision.status == "missing_in_dropbox":
                sync_candidates.append(decision)

        summary = dict(Counter(item.status for item in decisions))
        return DiffPlan(items=decisions, summary=summary, sync_candidates=sync_candidates)

    def _classify_decision(self, classified: ClassifiedEntry, dropbox_entry: InventoryEntry | None) -> DiffDecision:
        if classified.handling == "explicit_skip":
            return DiffDecision(entry=classified, status="explicit_skip", reason=classified.reason)

        if classified.handling != "sync":
            return DiffDecision(entry=classified, status="unsupported_for_first_pass", reason=classified.reason)

        if dropbox_entry is None:
            return DiffDecision(entry=classified, status="missing_in_dropbox", reason="path absent in Dropbox")

        if self._looks_like_same_file(classified.entry, dropbox_entry):
            return DiffDecision(
                entry=classified,
                status="exact_metadata_match_candidate",
                reason="same path, size and modified timestamp",
                matching_dropbox_entry=dropbox_entry,
            )

        return DiffDecision(
            entry=classified,
            status="path_exists_but_differs",
            reason="path exists in Dropbox but metadata differs",
            matching_dropbox_entry=dropbox_entry,
        )

    @staticmethod
    def _looks_like_same_file(yandex_entry: InventoryEntry, dropbox_entry: InventoryEntry) -> bool:
        if yandex_entry.size != dropbox_entry.size:
            return False
        if yandex_entry.modified_dt is None or dropbox_entry.modified_dt is None:
            return False
        return yandex_entry.modified_dt == dropbox_entry.modified_dt
