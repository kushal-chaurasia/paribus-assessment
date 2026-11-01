import asyncio
import json
import logging
from typing import Any

import aiohttp

from core.config import get_settings
from core.http import http_client

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class UpstreamError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HospitalAPI:
    """Client for the Hospital Directory API.

    Network errors and 5xx/429 responses are retried with exponential backoff;
    other 4xx responses fail immediately since retrying cannot fix them.
    """

    async def create_hospital(
        self, *, name: str, address: str, phone: str | None, batch_id: str
    ) -> dict[str, Any]:
        payload = {"name": name, "address": address, "phone": phone, "creation_batch_id": batch_id}
        return await self._request("POST", "/hospitals/", payload)

    async def list_batch(self, batch_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/hospitals/batch/{batch_id}") or []

    async def activate_batch(self, batch_id: str) -> None:
        await self._request("PATCH", f"/hospitals/batch/{batch_id}/activate")

    async def delete_batch(self, batch_id: str) -> None:
        await self._request("DELETE", f"/hospitals/batch/{batch_id}")

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        settings = get_settings()
        url = settings.hospital_api_base_url.rstrip("/") + path
        last_error, last_status = "", None

        for attempt in range(settings.upstream_max_retries + 1):
            if attempt:
                delay = settings.upstream_backoff_seconds * 2 ** (attempt - 1)
                logger.warning("Retrying %s %s in %.2fs (%s)", method, path, delay, last_error)
                await asyncio.sleep(delay)
            try:
                async with http_client.semaphore:
                    async with http_client.session.request(method, url, json=payload) as resp:
                        body = await resp.text()
                        if resp.status < 400:
                            return json.loads(body) if body else None
                        last_status = resp.status
                        last_error = f"{method} {path} returned {resp.status}: {body[:300]}"
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_status = None
                last_error = f"{method} {path} failed: {exc!r}"
                continue
            if last_status not in RETRYABLE_STATUSES:
                break

        raise UpstreamError(last_error, last_status)
