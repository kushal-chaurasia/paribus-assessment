from starlette.types import ASGIApp, Receive, Scope, Send

from core.database import db


class DBSessionMiddleware:
    """Opens one database session per HTTP request and binds it to ``db.session``.

    Sessions are lazy, so long-lived SSE streams hold no connection unless they
    use this session; the events route opens short scopes per poll instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        async with db():
            await self.app(scope, receive, send)
