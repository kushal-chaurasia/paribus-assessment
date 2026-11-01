from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint names so Alembic migrations stay reproducible across databases.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class MissingSessionError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("No database session in context; wrap the call in `async with db():`")


_current_session: ContextVar[AsyncSession | None] = ContextVar("db_session", default=None)


class DBProxy:
    """Global handle to the session bound to the current request or task.

    The session lives in a ContextVar, so services read ``db.session`` instead of
    having a session passed down through every call. HTTP requests get a scope from
    ``DBSessionMiddleware``; background tasks and SSE streams open their own with
    ``async with db():``.
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None

    def init(self, url: str) -> None:
        connect_args = {"timeout": 30} if url.startswith("sqlite") else {}
        self._engine = create_async_engine(url, connect_args=connect_args, pool_pre_ping=True)
        self._sessionmaker = async_sessionmaker(self._engine, expire_on_commit=False)

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("Database is not initialised; call db.init() first")
        return self._engine

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
        self._engine = None
        self._sessionmaker = None

    @property
    def session(self) -> AsyncSession:
        session = _current_session.get()
        if session is None:
            raise MissingSessionError()
        return session

    @asynccontextmanager
    async def __call__(self) -> AsyncGenerator[AsyncSession]:
        if self._sessionmaker is None:
            raise RuntimeError("Database is not initialised; call db.init() first")
        async with self._sessionmaker() as session:
            token = _current_session.set(session)
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                _current_session.reset(token)


db = DBProxy()
