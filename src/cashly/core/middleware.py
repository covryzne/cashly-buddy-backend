import logging
from time import perf_counter
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cashly.core.errors import error_response

logger = logging.getLogger("cashly.http")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started = perf_counter()
        status = 500
        response_started = False

        async def send_with_context(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers = [
                    (key, value) for key, value in headers if key.lower() != b"cache-control"
                ]
                message = {
                    **message,
                    "headers": [
                        *headers,
                        (b"x-request-id", request_id.encode()),
                        (b"cache-control", b"private, no-store"),
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_with_context)
        except Exception as exc:
            logger.error(
                "request_failed",
                extra={
                    "request_id": request_id,
                    "exception_type": type(exc).__name__,
                },
            )
            if response_started:
                raise
            response = error_response(
                request_id, 500, "INTERNAL_SERVER_ERROR", "An unexpected error occurred."
            )
            await response(scope, receive, send_with_context)
        finally:
            # Route template only: exclude raw URLs, IDs, query strings, headers and bodies.
            route = getattr(scope.get("route"), "path", "unmatched")
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": scope["method"],
                    "route": route,
                    "status": status,
                    "duration_ms": round((perf_counter() - started) * 1000, 2),
                },
            )
