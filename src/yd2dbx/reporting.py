from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from yd2dbx.models import DiffPlan, SyncOutcome


def build_report_bundle(plan: DiffPlan, sync_outcomes: list[SyncOutcome]) -> dict[str, object]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": plan.summary,
        "diff_items": [item.to_dict() for item in plan.items],
        "sync_outcomes": [outcome.to_dict() for outcome in sync_outcomes],
    }


def render_markdown_summary(payload: dict[str, object]) -> str:
    summary = payload.get("summary", {})
    grouped_items = _group_diff_items(payload.get("diff_items", []))
    sync_outcomes = payload.get("sync_outcomes", [])
    lines = [
        "# YD2DBX Summary",
        "",
        "| Bucket | Count |",
        "|--------|-------|",
    ]
    for key, value in sorted(summary.items()):
        lines.append(f"| {key} | {value} |")
    for key, items in sorted(grouped_items.items()):
        lines.extend(
            [
                "",
                f"## {key}",
                "",
            ]
        )
        for item in items:
            path = item.get("entry", {}).get("path", "<unknown>")
            reason = item.get("reason", "")
            lines.append(f"- `{path}`: {reason}")
    if isinstance(sync_outcomes, list) and sync_outcomes:
        lines.extend(["", "## sync_outcomes", ""])
        for item in sync_outcomes:
            if not isinstance(item, dict):
                continue
            path = item.get("path", "<unknown>")
            status = item.get("status", "unknown")
            detail = item.get("detail", "")
            lines.append(f"- `{path}`: {status} ({detail})")
    return "\n".join(lines) + "\n"


def write_reports(report_dir: str, report_name: str, payload: dict[str, object]) -> dict[str, str]:
    target_dir = Path(report_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    json_path = target_dir / f"{report_name}.json"
    md_path = target_dir / f"{report_name}.md"
    csv_path = target_dir / f"{report_name}.csv"

    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
    md_path.write_text(render_markdown_summary(payload))

    with csv_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["record_type", "status", "path", "detail"])
        for item in payload.get("diff_items", []):
            writer.writerow(["diff_item", item.get("status", ""), item.get("entry", {}).get("path", ""), item.get("reason", "")])
        for item in payload.get("sync_outcomes", []):
            writer.writerow(["sync_outcome", item.get("status", ""), item.get("path", ""), item.get("detail", "")])

    return {"json": str(json_path), "markdown": str(md_path), "csv": str(csv_path)}


def _group_diff_items(raw_items: object) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    if not isinstance(raw_items, list):
        return grouped
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "unknown"))
        grouped.setdefault(status, []).append(item)
    return grouped
