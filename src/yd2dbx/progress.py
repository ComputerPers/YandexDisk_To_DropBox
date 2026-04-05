from __future__ import annotations

import sys
import threading
from typing import TextIO


def format_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 999 * 3600:
        return "?"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    h, remainder = divmod(s, 3600)
    m = remainder // 60
    return f"{h}h{m:02d}m"


def format_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "░" * width
    ratio = min(done / total, 1.0)
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def format_sync_progress(
    done: int, total: int, synced: int, failed: int,
    elapsed_seconds: float,
) -> str:
    pct = done * 100 // total if total else 0
    bar = format_bar(done, total)
    parts = [f"{bar} {done}/{total} ({pct}%) ok:{synced} err:{failed}"]
    parts.append(format_eta(elapsed_seconds))
    if done > 0 and elapsed_seconds > 0:
        rate = elapsed_seconds / done
        remaining = rate * (total - done)
        parts.append(f"ETA ~{format_eta(remaining)}")
    return " | ".join(parts)


def format_progress_line(phase: str, detail: str = "") -> str:
    suffix = f" {detail}" if detail else ""
    return f"\r\033[K[{phase}]{suffix}"


def format_stage_line(phase: str, detail: str = "") -> str:
    suffix = f" {detail}" if detail else ""
    return f"[{phase}]{suffix}"


def print_phase(phase: str, detail: str = "", *, stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    print(format_progress_line(phase, detail), end="", flush=True, file=target)


def print_done(phase: str, detail: str = "", *, stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    print(format_progress_line(phase, detail), flush=True, file=target)


def print_stage(phase: str, detail: str = "", *, stream: TextIO | None = None) -> None:
    target = stream or sys.stdout
    print(format_stage_line(phase, detail), flush=True, file=target)


class MultiLineProgress:
    """Thread-safe progress display on separate lines using ANSI cursor control."""

    def __init__(self, labels: list[str], stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._lock = threading.Lock()
        self._label_to_row = {label: idx for idx, label in enumerate(labels)}
        self._total_rows = len(labels)
        self._latest: dict[str, str] = {label: "" for label in labels}
        for _ in range(self._total_rows):
            print(file=self._stream)

    def update(self, label: str, detail: str) -> None:
        row = self._label_to_row.get(label)
        if row is None:
            return
        self._latest[label] = detail
        with self._lock:
            up = self._total_rows - row
            self._stream.write(f"\033[{up}A\r\033[K[{label}] {detail}\033[{up}B\r")
            self._stream.flush()

    def finish(self, label: str, detail: str) -> None:
        row = self._label_to_row.get(label)
        if row is None:
            return
        self._latest[label] = detail
        with self._lock:
            up = self._total_rows - row
            self._stream.write(f"\033[{up}A\r\033[K[{label}] {detail}\033[{up}B\r")
            self._stream.flush()
