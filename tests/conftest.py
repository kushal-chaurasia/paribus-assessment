import asyncio
import itertools
import threading
from collections import deque
from pathlib import Path

import pytest
from aiohttp import web
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from core.config import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


class FakeUpstream:
    """In-process stand-in for the Hospital Directory API, served over real HTTP.

    Knobs:
      fail_names          -> POST returns 422 for these hospital names
      ghost_names         -> POST stores the hospital but returns 400 (lost response)
      create_failures     -> statuses returned by the next POSTs before succeeding
      activate_failures   -> statuses returned by the next activations before succeeding
    """

    def __init__(self) -> None:
        self.hospitals: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_names: set[str] = set()
        self.ghost_names: set[str] = set()
        self.create_failures: deque[int] = deque()
        self.activate_failures: deque[int] = deque()
        self._ids = itertools.count(101)
        self._loop = asyncio.new_event_loop()
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    def count(self, method: str, suffix: str = "") -> int:
        return sum(1 for m, path in self.calls if m == method and path.endswith(suffix))

    def in_batch(self, batch_id: str) -> list[dict]:
        return [h for h in self.hospitals if h["creation_batch_id"] == batch_id]

    @web.middleware
    async def _record(self, request: web.Request, handler):
        self.calls.append((request.method, request.path))
        return await handler(request)

    async def _create(self, request: web.Request) -> web.Response:
        body = await request.json()
        if self.create_failures:
            return web.json_response({"detail": "unavailable"}, status=self.create_failures.popleft())
        if body["name"] in self.fail_names:
            return web.json_response({"detail": "rejected"}, status=422)
        hospital = {"id": next(self._ids), **body, "active": False}
        self.hospitals.append(hospital)
        if body["name"] in self.ghost_names:
            return web.json_response({"detail": "response lost"}, status=400)
        return web.json_response(hospital)

    async def _list_batch(self, request: web.Request) -> web.Response:
        return web.json_response(self.in_batch(request.match_info["batch_id"]))

    async def _activate(self, request: web.Request) -> web.Response:
        if self.activate_failures:
            return web.json_response({"detail": "boom"}, status=self.activate_failures.popleft())
        batch = self.in_batch(request.match_info["batch_id"])
        for hospital in batch:
            hospital["active"] = True
        return web.json_response({"activated": len(batch)})

    async def _delete_batch(self, request: web.Request) -> web.Response:
        batch_id = request.match_info["batch_id"]
        before = len(self.hospitals)
        self.hospitals = [h for h in self.hospitals if h["creation_batch_id"] != batch_id]
        return web.json_response({"deleted": before - len(self.hospitals)})

    async def _start(self) -> None:
        app = web.Application(middlewares=[self._record])
        app.router.add_post("/hospitals/", self._create)
        app.router.add_get("/hospitals/batch/{batch_id}", self._list_batch)
        app.router.add_patch("/hospitals/batch/{batch_id}/activate", self._activate)
        app.router.add_delete("/hospitals/batch/{batch_id}", self._delete_batch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self._start(), self._loop).result(timeout=5)

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)


@pytest.fixture
def upstream():
    fake = FakeUpstream()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture(autouse=True)
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("UPSTREAM_BACKOFF_SECONDS", "0")
    monkeypatch.setenv("UPSTREAM_MAX_RETRIES", "2")
    monkeypatch.setenv("PROGRESS_POLL_INTERVAL_SECONDS", "0.01")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def alembic_config() -> Config:
    # env.py falls back to DATABASE_URL from settings, i.e. the per-test SQLite file.
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def migrated_db(alembic_config):
    command.upgrade(alembic_config, "head")


@pytest.fixture
def client(upstream, migrated_db, monkeypatch):
    from main import create_app

    monkeypatch.setenv("HOSPITAL_API_BASE_URL", upstream.base_url)
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client


def csv_file(content: str, filename: str = "hospitals.csv"):
    return {"file": (filename, content.encode(), "text/csv")}
