import asyncio
from contextlib import asynccontextmanager, suppress

import httpx2
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from cashly.api.push_test import router as push_test_router
from cashly.api.router import router
from cashly.core.environment import resolve_environment
from cashly.core.errors import register_error_handlers
from cashly.core.logging import configure_logging
from cashly.core.middleware import RequestContextMiddleware
from cashly.core.settings import Settings
from cashly.db.connection import Database
from cashly.schemas.errors import ErrorEnvelope
from cashly.services.notification_worker import run_notification_worker
from cashly.services.web_push import signing_key


def create_app(settings: Settings | None = None) -> FastAPI:
    """Factory: tests inject settings and never touch the user's env files."""
    settings = settings or Settings.from_selection(resolve_environment())
    configure_logging(settings.log_level, settings.log_format, settings.log_color)
    key = None
    if settings.web_push_private_key:
        key = signing_key(settings.web_push_private_key.get_secret_value())
        if not settings.web_push_subject.startswith(("mailto:", "https://")):
            raise ValueError("WEB_PUSH_SUBJECT must be a contact mailto: or HTTPS URI")

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
        app.state.web_push_key = key
        notification_task = (
            asyncio.create_task(
                run_notification_worker(
                    database,
                    key,
                    settings.web_push_subject,
                    staging=settings.environment == "staging",
                    production_times=(
                        settings.production_reminder_times
                        if settings.environment == "prod"
                        else None
                    ),
                )
            )
            if database is not None and settings.notification_worker_enabled
            else None
        )
        try:
            yield
        finally:
            if notification_task:
                notification_task.cancel()
                with suppress(asyncio.CancelledError):
                    await notification_task
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
    if settings.environment == "staging":
        app.include_router(push_test_router)
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
