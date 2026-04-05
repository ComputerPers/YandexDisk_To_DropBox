from __future__ import annotations

import logging
from time import monotonic, sleep
from typing import Callable

from yd2dbx.clients.dropbox import DropboxClient
from yd2dbx.clients.yandex_disk import YandexDiskClient
from yd2dbx.config import MigrationConfig
from yd2dbx.models import DiffDecision, SaveUrlJobStatus, SyncOutcome
from yd2dbx.paths import parent_folders
from yd2dbx.transport import AuthenticationError

logger = logging.getLogger(__name__)


_DROPBOX_UPLOAD_LIMIT = 150 * 1024 * 1024


class SyncRunner:
    def __init__(
        self,
        *,
        config: MigrationConfig,
        yandex_client: YandexDiskClient,
        dropbox_client: DropboxClient,
        sleep_func: Callable[[float], None] = sleep,
    ) -> None:
        self.config = config
        self.yandex_client = yandex_client
        self.dropbox_client = dropbox_client
        self.sleep_func = sleep_func

    def run(self, sync_candidates: list[DiffDecision]) -> list[SyncOutcome]:
        all_parents = parent_folders([c.entry.path for c in sync_candidates])
        if all_parents:
            self.dropbox_client.ensure_folders(all_parents)

        outcomes: list[SyncOutcome] = []
        for candidate in sync_candidates:
            outcomes.append(self.transfer_one(candidate))
        return outcomes

    def transfer_one(self, candidate: DiffDecision) -> SyncOutcome:
        path = candidate.entry.entry.path
        file_size = candidate.entry.entry.size
        last_error = "unknown error"
        for attempt in range(1, self.config.max_retries + 1):
            try:
                download_url = self.yandex_client.get_download_url(path)
                job_id = self.dropbox_client.save_url(path, download_url)
                status = self._wait_for_completion(job_id)
                if status.tag == "complete":
                    return SyncOutcome(path=path, status="synced", attempts=attempt, detail="server-side transfer completed")
                last_error = f"Dropbox save_url job ended with status {status.tag}"
            except AuthenticationError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                logger.warning("Attempt %d failed for %s: %s", attempt, path, last_error)
            if attempt < self.config.max_retries:
                self.sleep_func(self.config.poll_interval_seconds)

        logger.warning("Server-side transfer failed for %s after %d attempts, trying fallback",
                        path, self.config.max_retries)
        return self._fallback_local_transfer(path, file_size)

    def _wait_for_completion(self, job_id: str) -> SaveUrlJobStatus:
        deadline = monotonic() + self.config.max_polls * self.config.poll_interval_seconds
        while monotonic() < deadline:
            status = self.dropbox_client.check_save_url_job(job_id)
            if status.tag in {"complete", "failed"}:
                return status
            self.sleep_func(self.config.poll_interval_seconds)
        raise RuntimeError(f"poll timeout ({self.config.max_polls * self.config.poll_interval_seconds}s) for job {job_id}")

    def _fallback_local_transfer(self, path: str, file_size: int) -> SyncOutcome:
        """Download from Yandex locally, upload to Dropbox directly."""
        if file_size > _DROPBOX_UPLOAD_LIMIT:
            logger.warning("File too large for fallback: %s (%d bytes)", path, file_size)
            return SyncOutcome(
                path=path, status="review_required",
                attempts=self.config.max_retries + 1,
                detail=f"file too large for local fallback ({file_size} bytes > 150MB limit)",
            )
        try:
            data = self.yandex_client.download_file(path)
            self.dropbox_client.upload_small_file(path, data)
            return SyncOutcome(
                path=path, status="synced",
                attempts=self.config.max_retries + 1,
                detail="local fallback transfer completed",
            )
        except AuthenticationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("Fallback failed for %s: %s", path, exc)
            return SyncOutcome(
                path=path, status="review_required",
                attempts=self.config.max_retries + 1,
                detail=f"fallback also failed: {exc}",
            )

    _parent_folders_for = staticmethod(lambda path: parent_folders([path]))
