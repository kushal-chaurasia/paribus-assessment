import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from core.config import get_settings
from core.database import db
from core.http import http_client
from core.middleware import DBSessionMiddleware
from app.routes import bulk, health
from app.services import (
    InvalidCorrectionsError,
    JobConflictError,
    JobNotFoundError,
    UpstreamError,
    bulk_service,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    # Read at startup rather than import so environment overrides (tests, CLI) apply.
    settings = get_settings()
    db.init(settings.database_url)
    async with db():
        await bulk_service.recover_interrupted()
    await http_client.start(settings)
    try:
        yield
    finally:
        await bulk_service.shutdown()
        await http_client.close()
        await db.dispose()


def _error_handler(status_code: int):
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": str(exc)})

    return handler


async def _invalid_corrections(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, InvalidCorrectionsError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": str(exc),
            "errors": [error.model_dump() for error in exc.errors],
        },
    )


def create_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO)
    app = FastAPI(title=get_settings().app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(DBSessionMiddleware)
    app.add_exception_handler(JobNotFoundError, _error_handler(status.HTTP_404_NOT_FOUND))
    app.add_exception_handler(JobConflictError, _error_handler(status.HTTP_409_CONFLICT))
    app.add_exception_handler(InvalidCorrectionsError, _invalid_corrections)
    app.add_exception_handler(UpstreamError, _error_handler(status.HTTP_502_BAD_GATEWAY))
    app.include_router(health.router)
    app.include_router(bulk.router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port)
