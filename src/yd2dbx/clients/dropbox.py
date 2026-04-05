from __future__ import annotations

import json
import logging
import threading
from pathlib import PurePosixPath
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

from yd2dbx.models import InventoryEntry, Provider, SaveUrlJobStatus, normalize_path
from yd2dbx.transport import AuthenticationError, HttpApiError, JsonHttpTransport, Transport

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"


class DropboxClient:
    def __init__(
        self,
        token: str,
        transport: Transport | None = None,
        *,
        refresh_token: str = "",
        app_key: str = "",
        app_secret: str = "",
        on_token_refreshed: Callable[[str], None] | None = None,
    ) -> None:
        self.token = token
        self.transport = transport or JsonHttpTransport()
        self._refresh_token = refresh_token
        self._app_key = app_key
        self._app_secret = app_secret
        self._on_token_refreshed = on_token_refreshed
        self._refresh_lock = threading.Lock()
        self._token_version = 0
        if not self.token and self._can_refresh():
            self._do_refresh(0)

    def _can_refresh(self) -> bool:
        return bool(self._refresh_token and self._app_key and self._app_secret)

    def _do_refresh(self, expected_version: int) -> None:
        with self._refresh_lock:
            if self._token_version != expected_version:
                return
            body = urlencode({
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._app_key,
                "client_secret": self._app_secret,
            }).encode()
            req = Request(
                _TOKEN_URL,
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode())
            except HTTPError as exc:
                detail = exc.read().decode() if exc.fp else str(exc)
                raise AuthenticationError(
                    f"Dropbox token refresh failed (HTTP {exc.code}): {detail}"
                ) from exc
            new_token = result.get("access_token", "")
            if not new_token:
                raise AuthenticationError("Dropbox token refresh: no access_token in response")
            self.token = new_token
            self._token_version += 1
            logger.info("Dropbox token refreshed (version=%d)", self._token_version)
            if self._on_token_refreshed:
                self._on_token_refreshed(new_token)

    def _api(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, object] | None = None,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Wrap transport.request with automatic token refresh on 401."""
        with self._refresh_lock:
            version = self._token_version
            headers = self._headers()
        try:
            return self.transport.request(method, url, headers=headers, json_body=json_body, params=params)
        except AuthenticationError:
            if not self._can_refresh():
                raise
            self._do_refresh(version)
            with self._refresh_lock:
                headers = self._headers()
            return self.transport.request(method, url, headers=headers, json_body=json_body, params=params)

    def check_read_access(self) -> None:
        self._ensure_token()
        self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/list_folder",
            json_body={"path": "", "recursive": False, "include_deleted": False},
        )

    def check_write_access(self) -> None:
        self._ensure_token()
        temp_path = f"/.yd2dbx-access-check-{uuid4().hex}"
        self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/create_folder_v2",
            json_body={"path": temp_path, "autorename": False},
        )
        self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/delete_v2",
            json_body={"path": temp_path},
        )

    def list_all_files(self, root_path: str = "") -> list[InventoryEntry]:
        items, cursor, has_more = self.list_folder_start(root_path)
        while has_more:
            page_items, cursor, has_more = self.list_folder_continue(str(cursor))
            items.extend(page_items)
        return items

    def list_folder_children(self, path: str = "") -> tuple[list[InventoryEntry], list[str]]:
        """Non-recursive listing: returns (files, subfolder_paths)."""
        all_files: list[InventoryEntry] = []
        all_folders: list[str] = []
        payload = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/list_folder",
            json_body={"path": path, "recursive": False, "include_deleted": False},
        )
        files, folders = self._split_entries_and_folders(payload.get("entries", []))
        all_files.extend(files)
        all_folders.extend(folders)
        cursor = _as_cursor(payload.get("cursor"))
        while payload.get("has_more") and cursor:
            payload = self._api(
                "POST",
                "https://api.dropboxapi.com/2/files/list_folder/continue",
                json_body={"cursor": cursor},
            )
            files, folders = self._split_entries_and_folders(payload.get("entries", []))
            all_files.extend(files)
            all_folders.extend(folders)
            cursor = _as_cursor(payload.get("cursor"))
        return all_files, all_folders

    def list_folder_start(self, root_path: str = "") -> tuple[list[InventoryEntry], str | None, bool]:
        payload = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/list_folder",
            json_body={"path": root_path, "recursive": True, "include_deleted": False},
        )
        return self._parse_entries(payload.get("entries", [])), _as_cursor(payload.get("cursor")), bool(payload.get("has_more"))

    def list_folder_continue(self, cursor: str) -> tuple[list[InventoryEntry], str | None, bool]:
        payload = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/list_folder/continue",
            json_body={"cursor": cursor},
        )
        return self._parse_entries(payload.get("entries", [])), _as_cursor(payload.get("cursor")), bool(payload.get("has_more"))

    def ensure_folders(self, folders: list[str]) -> None:
        seen: set[str] = set()
        ordered = sorted((folder for folder in folders if folder and folder != "/"), key=lambda value: (value.count("/"), value))
        for folder in ordered:
            normalized = normalize_path(folder)
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                self._api(
                    "POST",
                    "https://api.dropboxapi.com/2/files/create_folder_v2",
                    json_body={"path": normalized, "autorename": False},
                )
            except HttpApiError as exc:
                if exc.status_code != 409:
                    raise

    def create_folder_batch(self, paths: list[str]) -> str | None:
        """Submit batch folder creation. Returns async job_id or None if sync."""
        data = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/create_folder_batch",
            json_body={"paths": paths, "autorename": False, "force_async": False},
        )
        tag = data.get(".tag")
        if tag == "complete":
            return None
        return str(data.get("async_job_id", ""))

    def check_folder_batch_job(self, job_id: str) -> str:
        """Poll batch folder job. Returns 'complete', 'in_progress', or 'failed'."""
        data = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/create_folder_batch/check",
            json_body={"async_job_id": job_id},
        )
        return str(data.get(".tag", "failed"))

    def save_url(self, path: str, url: str) -> str:
        payload = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/save_url",
            json_body={"path": path, "url": url},
        )
        job_id = payload.get("async_job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError(f"Dropbox did not return async_job_id for {path}")
        return job_id

    def check_save_url_job(self, job_id: str) -> SaveUrlJobStatus:
        payload = self._api(
            "POST",
            "https://api.dropboxapi.com/2/files/save_url/check_job_status",
            json_body={"async_job_id": job_id},
        )
        tag = payload.get(".tag")
        if not isinstance(tag, str) or not tag:
            raise RuntimeError(f"Dropbox did not return job status for {job_id}")
        metadata = payload.get("metadata")
        return SaveUrlJobStatus(tag=tag, metadata=metadata if isinstance(metadata, dict) else {})

    def _split_entries_and_folders(
        self, raw_entries: list[dict[str, object]],
    ) -> tuple[list[InventoryEntry], list[str]]:
        files = self._parse_entries(raw_entries)
        folders = [
            str(raw.get("path_display") or raw.get("path_lower") or "")
            for raw in raw_entries
            if raw.get(".tag") == "folder"
        ]
        return files, folders

    def _parse_entries(self, raw_entries: list[dict[str, object]]) -> list[InventoryEntry]:
        items: list[InventoryEntry] = []
        for raw in raw_entries:
            if raw.get(".tag") != "file":
                continue
            items.append(
                InventoryEntry(
                    provider=Provider.DROPBOX,
                    path=str(raw.get("path_display") or raw.get("path_lower") or ""),
                    size=int(raw.get("size", 0)),
                    modified=str(raw.get("server_modified")) if raw.get("server_modified") else None,
                    mime_type=None,
                    source_hash=str(raw.get("content_hash")) if raw.get("content_hash") else None,
                    source_hash_type="content_hash",
                )
            )
        return items

    def parent_folders_for(self, path: str) -> list[str]:
        parts = PurePosixPath(normalize_path(path)).parts[:-1]
        current = ""
        folders: list[str] = []
        for part in parts:
            if part == "/":
                continue
            current = f"{current}/{part}"
            folders.append(current)
        return folders

    def upload_small_file(self, path: str, data: bytes) -> None:
        """Upload file content directly (for files up to 150MB)."""
        try:
            self._upload_raw(path, data)
        except HTTPError as exc:
            if exc.code != 401:
                raise
            if not self._can_refresh():
                raise AuthenticationError(
                    f"Dropbox upload 401 for {path}: token expired"
                ) from exc
            self._do_refresh(self._token_version)
            self._upload_raw(path, data)

    def _upload_raw(self, path: str, data: bytes) -> None:
        api_arg = json.dumps({"path": normalize_path(path), "mode": "overwrite", "mute": True})
        req = Request(
            "https://content.dropboxapi.com/2/files/upload",
            data=data,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/octet-stream",
                "Dropbox-API-Arg": api_arg,
            },
            method="POST",
        )
        with urlopen(req, timeout=90) as resp:
            resp.read()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _ensure_token(self) -> None:
        if not self.token and not self._can_refresh():
            raise AuthenticationError(
                "Dropbox token is missing. Запустите ./yd2dbx setup-dropbox "
                "или положите access token в .dropbox"
            )


def _as_cursor(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None
