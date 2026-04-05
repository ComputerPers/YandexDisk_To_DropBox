from __future__ import annotations

from urllib.request import urlopen

from yd2dbx.models import InventoryEntry, Provider
from yd2dbx.transport import JsonHttpTransport, Transport


class YandexDiskClient:
    def __init__(self, token: str, transport: Transport | None = None) -> None:
        self.token = token
        self.transport = transport or JsonHttpTransport()

    def check_read_access(self) -> None:
        self._ensure_token("Yandex Disk")
        self.transport.request(
            "GET",
            "https://cloud-api.yandex.net/v1/disk",
            headers=self._headers(),
        )

    def list_all_files(self, page_size: int = 1000) -> list[InventoryEntry]:
        items: list[InventoryEntry] = []
        offset = 0
        while True:
            page_items, has_more = self.list_files_page(offset=offset, page_size=page_size)
            items.extend(page_items)
            if not has_more:
                break
            offset += page_size
        return items

    def list_files_page(
        self, offset: int = 0, page_size: int = 1000, media_type: str | None = None,
    ) -> tuple[list[InventoryEntry], bool]:
        params: dict[str, object] = {
            "limit": page_size,
            "offset": offset,
            "fields": "items.path,items.size,items.modified,items.mime_type,items.md5,items.type",
        }
        if media_type is not None:
            params["media_type"] = media_type
        payload = self.transport.request(
            "GET",
            "https://cloud-api.yandex.net/v1/disk/resources/files",
            headers=self._headers(),
            params=params,
        )
        raw_items = payload.get("items", [])
        items: list[InventoryEntry] = []
        for raw in raw_items:
            if raw.get("type") == "file":
                items.append(
                    InventoryEntry(
                        provider=Provider.YANDEX,
                        path=raw["path"],
                        size=int(raw.get("size", 0)),
                        modified=raw.get("modified"),
                        mime_type=raw.get("mime_type"),
                        source_hash=raw.get("md5"),
                        source_hash_type="md5",
                    )
                )
        return items, len(raw_items) >= page_size

    def get_download_url(self, path: str) -> str:
        payload = self.transport.request(
            "GET",
            "https://cloud-api.yandex.net/v1/disk/resources/download",
            headers=self._headers(),
            params={"path": path},
        )
        href = payload.get("href")
        if not isinstance(href, str) or not href:
            raise RuntimeError(f"Yandex Disk did not return download URL for {path}")
        return href

    def download_file(self, path: str) -> bytes:
        """Download file content via temporary URL. For small files fallback."""
        url = self.get_download_url(path)
        with urlopen(url, timeout=90) as resp:
            return resp.read()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self.token}"} if self.token else {}

    def _ensure_token(self, provider_name: str) -> None:
        if not self.token:
            raise RuntimeError(f"{provider_name} token is missing. Set YANDEX_DISK_TOKEN or create .yadisk file.")
