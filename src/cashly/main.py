from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from cashly.api.router import router
from cashly.core.environment import resolve_environment
from cashly.core.errors import register_error_handlers
from cashly.core.logging import configure_logging
from cashly.core.middleware import RequestContextMiddleware
from cashly.core.settings import Settings
from cashly.db.connection import Database
from cashly.schemas.errors import ErrorEnvelope


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory: tests inject settings and never touch the user's env files."""
    settings = settings or Settings.from_selection(resolve_environment())
    configure_logging(settings.log_level, settings.log_format, settings.log_color)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings) if settings.database_url else None
        app.state.database = database
        app.state.auth_http = httpx2.AsyncClient(
            timeout=10, follow_redirects=False, trust_env=False
        )
        app.state.ai_http = httpx2.AsyncClient(
            timeout=settings.gemini_timeout_seconds, follow_redirects=False, trust_env=False
        )
        app.state.chat_limits = {}
        try:
            yield
        finally:
            await app.state.auth_http.aclose()
            await app.state.ai_http.aclose()
            if database is not None:
                database.dispose()

    app = FastAPI(
        title="Cashly Finance API",
        version="0.1.0",
        description="Google identity verification, account bootstrap and expense reads.",
        docs_url="/docs" if settings.environment in {"dev", "staging"} else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.environment in {"dev", "staging"} else None,
        responses={422: {"model": ErrorEnvelope}},
        lifespan=lifespan,
    )
    app.state.settings = settings
    register_error_handlers(app)
    app.include_router(router)
    # Context catches failures before they reach ServerErrorMiddleware; CORS wraps
    # context so even its generated 500 responses carry the origin allowlist.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Server-Timing"],
    )
    return app
