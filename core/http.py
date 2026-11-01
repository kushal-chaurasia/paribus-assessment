import asyncio

import aiohttp

from core.config import Settings


class HTTPClient:
    """Owns the app-wide aiohttp session and the upstream concurrency limit."""

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._semaphore: asyncio.Semaphore | None = None

    async def start(self, settings: Settings) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.upstream_timeout_seconds),
            connector=aiohttp.TCPConnector(limit=settings.upstream_concurrency),
        )
        self._semaphore = asyncio.Semaphore(settings.upstream_concurrency)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None
        self._semaphore = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("HTTP client is not started")
        return self._session

    @property
    def semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            raise RuntimeError("HTTP client is not started")
        return self._semaphore


http_client = HTTPClient()
