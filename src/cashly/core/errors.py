from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from cashly.schemas.errors import ErrorBody, ErrorEnvelope


class ApiError(HTTPException):
    """Only application-authored public messages, never upstream exception text."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status)
        self.code = code
        self.public_message = message


def error_response(
    request_id: str,
    status: int,
    code: str,
    message: str,
    details: dict | None = None,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorEnvelope(
            error=ErrorBody(
                code=code,
                message=message,
                details=details or {},
                request_id=request_id,
            )
        ).model_dump(),
        headers=headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc, ApiError):
            return error_response(
                request.state.request_id,
                exc.status_code,
                exc.code,
                exc.public_message,
                headers=exc.headers,
            )
        codes = {
            404: "RESOURCE_NOT_FOUND",
            405: "METHOD_NOT_ALLOWED",
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            429: "RATE_LIMITED",
        }
        # Exception detail may contain private data; never echo it by default.
        return error_response(
            request.state.request_id,
            exc.status_code,
            codes.get(exc.status_code, "HTTP_ERROR"),
            HTTPStatus(exc.status_code).phrase,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never serialize `input`, ctx, request body, or exception strings.
        issues = [{"location": list(issue["loc"]), "type": issue["type"]} for issue in exc.errors()]
        return error_response(
            request.state.request_id,
            422,
            "VALIDATION_ERROR",
            "Request validation failed.",
            {"issues": issues},
        )
