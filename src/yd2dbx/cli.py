from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from yd2dbx.classifier import FileClassifier
from yd2dbx.clients.dropbox import DropboxClient
from yd2dbx.clients.yandex_disk import YandexDiskClient
from yd2dbx.config import MigrationConfig
from yd2dbx.db import MigrationDB
from yd2dbx.diff_engine import DiffEngine
from yd2dbx.models import InventoryEntry, Provider
from yd2dbx.paths import filter_to_root
from yd2dbx.reporting import build_report_bundle, render_markdown_summary, write_reports
from yd2dbx.runner import MigrationRunner
from yd2dbx.sync_runner import SyncRunner
from yd2dbx.transport import AuthenticationError


def _make_dropbox_client(config: MigrationConfig) -> DropboxClient:
    on_refreshed = None
    if config.dropbox_token_file and config.dropbox_refresh_token:
        token_file = config.dropbox_token_file

        def _persist_token(new_token: str) -> None:
            Path(token_file).write_text(
                f"app_key={config.dropbox_app_key}\n"
                f"app_secret={config.dropbox_app_secret}\n"
                f"refresh_token={config.dropbox_refresh_token}\n"
                f"access_token={new_token}\n"
            )

        on_refreshed = _persist_token

    return DropboxClient(
        config.dropbox_token,
        refresh_token=config.dropbox_refresh_token,
        app_key=config.dropbox_app_key,
        app_secret=config.dropbox_app_secret,
        on_token_refreshed=on_refreshed,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yd2dbx", description="Safe-first Yandex Disk to Dropbox migration helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inventory", help="Collect raw inventories from Yandex Disk and Dropbox")
    diff_parser = subparsers.add_parser("diff", help="Build diff report from live inventories")
    diff_parser.add_argument("--inventory-json", help="Reuse inventories saved by the inventory command")

    sync_parser = subparsers.add_parser("sync", help="Run primary document sync")
    sync_parser.add_argument("--execute", action="store_true", help="Perform writes to Dropbox")
    sync_parser.add_argument("--inventory-json", help="Reuse inventories saved by the inventory command")
    run_parser = subparsers.add_parser("run", help="Full automatic migration")
    run_parser.add_argument("--db", default=".yd2dbx.db", help="Path to state database")
    run_parser.add_argument("--reset", action="store_true", help="Start fresh, delete existing DB")

    subparsers.add_parser("setup-dropbox", help="Interactive Dropbox OAuth setup (refresh token)")

    report_parser = subparsers.add_parser("report", help="Render markdown summary from JSON payload")
    report_parser.add_argument("payload", help="Path to report JSON file")
    return parser


def _setup_logging() -> None:
    """Configure yd2dbx logging: INFO+ to timestamped file, nothing to console.

    Console is reserved for the progress bar. All diagnostic output goes to
    logs/run_<timestamp>.txt. Set YD2DBX_LOG_LEVEL=WARNING (or DEBUG/INFO)
    to also mirror logs to stderr if needed for debugging.
    """
    from datetime import datetime, timezone

    app_logger = logging.getLogger("yd2dbx")
    if app_logger.handlers:
        return
    app_logger.setLevel(logging.DEBUG)
    app_logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s",
                            datefmt="%H:%M:%S")

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"run_{timestamp}.txt"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(fmt)
    app_logger.addHandler(file_handler)

    console_level = os.environ.get("YD2DBX_LOG_LEVEL", "").upper()
    if console_level:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(getattr(logging, console_level, logging.WARNING))
        console_handler.setFormatter(fmt)
        app_logger.addHandler(console_handler)


def main(argv: list[str] | None = None) -> int:

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "setup-dropbox":
            return _setup_dropbox()

        config = MigrationConfig.from_env()

        if args.command == "report":
            payload = json.loads(Path(args.payload).read_text())
            print(render_markdown_summary(payload))
            return 0

        if args.command == "run":
            db_path = Path(args.db)
            if args.reset and db_path.exists():
                db_path.unlink()

            db = MigrationDB(args.db)
            try:
                yandex = YandexDiskClient(config.yandex_token)
                dropbox = _make_dropbox_client(config)
                runner = MigrationRunner(
                    config=config,
                    db=db,
                    yandex=yandex,
                    dropbox=dropbox,
                    classifier=FileClassifier(config),
                    sync_runner=SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox),
                )
                return runner.run()
            finally:
                db.close()

        classifier = FileClassifier(config)
        engine = DiffEngine()

        if args.command == "inventory":
            yandex = YandexDiskClient(config.yandex_token)
            dropbox = _make_dropbox_client(config)
            _check_read_access(yandex, dropbox)
            yandex_inventory = filter_to_root(yandex.list_all_files(), config.root_path)
            dropbox_inventory = filter_to_root(dropbox.list_all_files(""), config.root_path)
            payload = {
                "yandex_inventory": [entry.to_dict() for entry in yandex_inventory],
                "dropbox_inventory": [entry.to_dict() for entry in dropbox_inventory],
            }
            write_reports(config.report_dir, "inventory", {"summary": {"yandex": len(yandex_inventory), "dropbox": len(dropbox_inventory)}, **payload})
            return 0

        if getattr(args, "inventory_json", None):
            yandex_inventory, dropbox_inventory = _load_inventory_report(args.inventory_json)
            yandex_inventory = filter_to_root(yandex_inventory, config.root_path)
            dropbox_inventory = filter_to_root(dropbox_inventory, config.root_path)
        else:
            yandex = YandexDiskClient(config.yandex_token)
            dropbox = _make_dropbox_client(config)
            _check_read_access(yandex, dropbox)
            yandex_inventory = filter_to_root(yandex.list_all_files(), config.root_path)
            dropbox_inventory = filter_to_root(dropbox.list_all_files(""), config.root_path)

        classified = [classifier.classify(entry) for entry in yandex_inventory]
        plan = engine.build_plan(classified, dropbox_inventory)

        if args.command == "diff":
            payload = build_report_bundle(plan, [])
            write_reports(config.report_dir, "diff", payload)
            return 0

        execute = args.execute
        if not execute:
            payload = build_report_bundle(plan, [])
            write_reports(config.report_dir, "sync-dry-run", payload)
            return 0

        yandex = YandexDiskClient(config.yandex_token)
        dropbox = _make_dropbox_client(config)
        _check_read_access(yandex, dropbox)
        dropbox.check_write_access()
        runner = SyncRunner(config=config, yandex_client=yandex, dropbox_client=dropbox)
        outcomes = runner.run(plan.sync_candidates)
        payload = build_report_bundle(plan, outcomes)
        write_reports(config.report_dir, "sync", payload)
        return 0
    except AuthenticationError as exc:
        print(f"\nAUTH ERROR: {exc}", file=sys.stderr)
        print("", file=sys.stderr)
        msg = str(exc).lower()
        if "yandex" in msg or "cloud-api.yandex" in msg:
            print(
                "  Yandex Disk: получите новый токен на https://oauth.yandex.ru\n"
                "               и обновите файл .yadisk",
                file=sys.stderr,
            )
        if "dropbox" in msg or "api.dropbox" in msg:
            print(
                "  Dropbox:     запустите ./yd2dbx setup-dropbox\n"
                "               или получите новый токен на https://www.dropbox.com/developers/apps",
                file=sys.stderr,
            )
        if "yandex" not in msg and "dropbox" not in msg:
            print(
                "  Yandex Disk: получите новый токен на https://oauth.yandex.ru → .yadisk\n"
                "  Dropbox:     запустите ./yd2dbx setup-dropbox\n"
                "               или https://www.dropbox.com/developers/apps → .dropbox",
                file=sys.stderr,
            )
        print(
            "\n  После обновления токена: ./yd2dbx run",
            file=sys.stderr,
        )
        return 2
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _setup_dropbox() -> int:
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    print("=== Настройка Dropbox OAuth (refresh token) ===")
    print()
    print("Вам нужны App Key и App Secret из Dropbox App Console.")
    print("Откройте https://www.dropbox.com/developers/apps")
    print("и выберите ваше приложение (вкладка Settings).")
    print()

    app_key = input("App Key: ").strip()
    app_secret = input("App Secret: ").strip()

    if not app_key or not app_secret:
        print("ERROR: App Key и App Secret обязательны", file=sys.stderr)
        return 1

    auth_url = (
        f"https://www.dropbox.com/oauth2/authorize"
        f"?client_id={app_key}"
        f"&response_type=code"
        f"&token_access_type=offline"
    )
    print()
    print("Откройте эту ссылку в браузере и подтвердите доступ:")
    print()
    print(f"  {auth_url}")
    print()
    print("Скопируйте полученный код авторизации сюда.")
    print()

    code = input("Код авторизации: ").strip()
    if not code:
        print("ERROR: Код авторизации обязателен", file=sys.stderr)
        return 1

    body = urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": app_key,
        "client_secret": app_secret,
    }).encode()
    req = Request(
        "https://api.dropboxapi.com/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except HTTPError as exc:
        detail = exc.read().decode() if exc.fp else str(exc)
        print(f"ERROR: Не удалось обменять код на токен (HTTP {exc.code}): {detail}", file=sys.stderr)
        return 1

    refresh_token = result.get("refresh_token", "")
    access_token = result.get("access_token", "")

    if not refresh_token:
        print(
            "ERROR: Dropbox не вернул refresh_token. "
            "Убедитесь что в URL есть token_access_type=offline.",
            file=sys.stderr,
        )
        return 1

    dropbox_path = Path(".dropbox")
    content = (
        f"app_key={app_key}\n"
        f"app_secret={app_secret}\n"
        f"refresh_token={refresh_token}\n"
    )
    if access_token:
        content += f"access_token={access_token}\n"
    dropbox_path.write_text(content)

    print()
    print(f"Refresh token сохранён в {dropbox_path}")
    if access_token:
        print(f"Access token: {access_token[:20]}...")
    print()
    print("Теперь можно запускать: ./yd2dbx run")
    print("Токен будет автоматически обновляться при истечении.")
    return 0


def _load_inventory_report(path: str) -> tuple[list[InventoryEntry], list[InventoryEntry]]:
    payload = json.loads(Path(path).read_text())
    yandex_items = [_inventory_entry_from_dict(item) for item in payload.get("yandex_inventory", [])]
    dropbox_items = [_inventory_entry_from_dict(item) for item in payload.get("dropbox_inventory", [])]
    return yandex_items, dropbox_items


def _inventory_entry_from_dict(raw: dict[str, object]) -> InventoryEntry:
    return InventoryEntry(
        provider=Provider(str(raw.get("provider", "yandex"))),
        path=str(raw.get("path", "")),
        size=int(raw.get("size", 0)),
        modified=str(raw.get("modified")) if raw.get("modified") else None,
        mime_type=str(raw.get("mime_type")) if raw.get("mime_type") else None,
        source_hash=str(raw.get("source_hash")) if raw.get("source_hash") else None,
        source_hash_type=str(raw.get("source_hash_type")) if raw.get("source_hash_type") else None,
    )


def _check_read_access(yandex: YandexDiskClient, dropbox: DropboxClient) -> None:
    yandex.check_read_access()
    dropbox.check_read_access()


if __name__ == "__main__":
    _setup_logging()
    raise SystemExit(main())
