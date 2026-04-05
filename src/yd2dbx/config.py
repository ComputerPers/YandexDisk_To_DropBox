from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"Expected integer, got {value!r}") from None


def _read_secret_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text().strip()


def _read_dropbox_config(path: Path) -> dict[str, str]:
    """Parse .dropbox file.

    Supports two formats:
    - Legacy: single access token on one line.
    - Key=value: lines like ``app_key=X``, ``app_secret=Y``, ``refresh_token=Z``.
    """
    if not path.exists():
        return {}
    content = path.read_text().strip()
    if not content:
        return {}
    lines = content.splitlines()
    if any("=" in line for line in lines):
        result: dict[str, str] = {}
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                result[key.strip()] = value.strip()
        return result
    return {"token": content}


@dataclass(slots=True)
class MigrationConfig:
    yandex_token: str = ""
    dropbox_token: str = ""
    dropbox_refresh_token: str = ""
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_token_file: str = ""
    root_path: str = "/"
    report_dir: str = "reports"
    dry_run: bool = True
    large_file_threshold_bytes: int = 256 * 1024 * 1024
    max_retries: int = 3
    max_polls: int = 12
    poll_interval_seconds: int = 5
    screenshot_markers: tuple[str, ...] = field(
        default_factory=lambda: (
            "screenshot",
            "screen shot",
            "screen_shot",
            "скриншот",
        )
    )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, base_dir: Path | None = None) -> "MigrationConfig":
        values = dict(os.environ if env is None else env)
        root = base_dir or Path.cwd()
        threshold_mb = _as_int(values.get("YD2DBX_LARGE_FILE_THRESHOLD_MB"), 256)

        dbx = _read_dropbox_config(root / ".dropbox")
        dbx_token_file = str(root / ".dropbox") if (root / ".dropbox").exists() else ""

        return cls(
            yandex_token=_read_secret_file(root / ".yadisk") or values.get("YANDEX_DISK_TOKEN", ""),
            dropbox_token=dbx.get("token", "") or dbx.get("access_token", "") or values.get("DROPBOX_TOKEN", ""),
            dropbox_refresh_token=dbx.get("refresh_token", ""),
            dropbox_app_key=dbx.get("app_key", ""),
            dropbox_app_secret=dbx.get("app_secret", ""),
            dropbox_token_file=dbx_token_file,
            root_path=values.get("YD2DBX_ROOT", "/"),
            report_dir=values.get("YD2DBX_REPORT_DIR", "reports"),
            dry_run=_as_bool(values.get("YD2DBX_DRY_RUN"), True),
            large_file_threshold_bytes=threshold_mb * 1024 * 1024,
            max_retries=_as_int(values.get("YD2DBX_MAX_RETRIES"), 3),
            max_polls=_as_int(values.get("YD2DBX_MAX_POLLS"), 300),
            poll_interval_seconds=_as_int(values.get("YD2DBX_POLL_INTERVAL_SECONDS"), 2),
        )
