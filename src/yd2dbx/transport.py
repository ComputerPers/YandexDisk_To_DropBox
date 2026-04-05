from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from time import sleep
from typing import Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class HttpApiError(RuntimeError):
    """Raised for non-2xx HTTP responses with structured status code access."""

    def __init__(self, status_code: int, url: str, detail: str) -> None:
        self.status_code = status_code
        self.url = url
        self.detail_body = detail
        super().__init__(f"HTTP {status_code} for {url}: {detail}")


class AuthenticationError(RuntimeError):
    """Raised when an API returns 401 and the token cannot be refreshed."""


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]: ...


@dataclass(slots=True)
class JsonHttpTransport:
    timeout_seconds: int = 90
    max_retries_on_rate_limit: int = 5
    sleep_func: Callable[[float], None] = field(default=sleep, repr=False)
    on_rate_limit: Callable[[], None] | None = field(default=None, repr=False)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        request_headers = dict(headers or {})
        full_url = self._build_url(url, params)
        body = None
        if json_body is not None:
            request_headers.setdefault("Content-Type", "application/json")
            body = json.dumps(json_body).encode()

        for attempt in range(self.max_retries_on_rate_limit + 1):
            req = Request(full_url, data=body, headers=request_headers, method=method.upper())
            try:
                with urlopen(req, timeout=self.timeout_seconds) as response:
                    payload = response.read().decode() or "{}"
                    return json.loads(payload)
            except HTTPError as exc:
                if exc.code == 429 and attempt < self.max_retries_on_rate_limit:
                    raw = exc.headers.get("Retry-After", "5")
                    try:
                        wait = min(int(raw), 60)
                    except (ValueError, TypeError):
                        wait = 5
                    logger.warning("429 rate-limited on %s, retry after %ds", full_url, wait)
                    if self.on_rate_limit:
                        self.on_rate_limit()
                    self.sleep_func(wait)
                    continue
                payload = exc.read().decode() or "{}"
                detail = payload
                try:
                    parsed = json.loads(payload)
                    detail = json.dumps(parsed, sort_keys=True)
                except json.JSONDecodeError:
                    pass
                logger.error("HTTP %d for %s: %s", exc.code, full_url, detail[:200])
                if exc.code == 401:
                    raise AuthenticationError(f"HTTP 401 for {full_url}: {detail}") from exc
                raise HttpApiError(exc.code, full_url, detail) from exc
            except TimeoutError as exc:
                logger.error("Timeout on %s after %ds", full_url, self.timeout_seconds)
                raise RuntimeError(f"Request to {full_url} timed out after {self.timeout_seconds}s") from exc

        raise RuntimeError(f"Rate limit (429) persisted after {self.max_retries_on_rate_limit} retries for {full_url}")

    @staticmethod
    def _build_url(url: str, params: dict[str, object] | None) -> str:
        if not params:
            return url
        encoded = urlencode({key: value for key, value in params.items() if value is not None})
        separator = "&" if "?" in url else "?"
        return f"{url}{separator}{encoded}"
