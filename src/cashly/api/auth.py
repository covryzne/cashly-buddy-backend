import base64
import json
from urllib.parse import urlsplit

import httpx2
from fastapi import APIRouter, Request, Response
from sqlalchemy.exc import SQLAlchemyError

from cashly.core.auth import verify_access_token
from cashly.core.errors import ApiError
from cashly.schemas.auth import (
    AuthConfiguration,
    AuthData,
    AuthEnvelope,
    AuthSession,
    EmailVerificationRequest,
    LoginRequest,
    RegisterRequest,
    VerifyEmailRequest,
)
from cashly.services.accounts import bootstrap

router = APIRouter(prefix="/auth", tags=["Authentication"])


def authority(request: Request) -> tuple[str, str]:
    settings = request.app.state.settings
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise ApiError(503, "AUTH_NOT_CONFIGURED", "Supabase Auth backend belum dikonfigurasi.")
    return settings.supabase_url, settings.supabase_publishable_key.get_secret_value()


@router.get(
    "/configuration",
    response_model=AuthConfiguration,
    summary="Public Auth connection metadata for the Next.js server",
)
def configuration(request: Request) -> AuthConfiguration:
    url, key = authority(request)
    # Only publishable/legacy anon keys may leave backend configuration.
    publishable = key.startswith("sb_publishable_")
    if not publishable:
        try:
            part = key.split(".")[1]
            publishable = (
                json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("role")
                == "anon"
            )
        except (ValueError, IndexError, TypeError):
            publishable = False
    if not publishable:
        raise ApiError(
            503,
            "AUTH_KEY_INVALID",
            "Gunakan publishable key atau anon key, bukan secret/service-role key.",
        )
    return AuthConfiguration(
        supabase_url=url,
        publishable_key=key,
        local=urlsplit(url).hostname in {"localhost", "127.0.0.1", "::1"},
    )


async def password_action(request: Request, body: LoginRequest, *, register: bool) -> AuthEnvelope:
    url, key = authority(request)
    payload = {"email": body.email, "password": body.password.get_secret_value()}
    if register:
        payload["data"] = {"full_name": body.name}
    path = "/signup" if register else "/token?grant_type=password"
    try:
        upstream = await request.app.state.auth_http.post(
            url + "/auth/v1" + path,
            headers={"apikey": key},
            json=payload,
        )
    except httpx2.HTTPError:
        raise ApiError(
            503, "AUTH_UNAVAILABLE", "Layanan autentikasi belum dapat dihubungi."
        ) from None
    if upstream.status_code == 429:
        raise ApiError(429, "AUTH_RATE_LIMITED", "Terlalu banyak percobaan. Tunggu sebentar.")
    if upstream.status_code >= 500:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Layanan autentikasi sedang tidak tersedia.")
    if not upstream.is_success:
        if not register:
            try:
                unconfirmed = upstream.json().get("code") == "email_not_confirmed"
            except (ValueError, AttributeError):
                unconfirmed = False
            if unconfirmed:
                raise ApiError(403, "EMAIL_NOT_VERIFIED", "Verifikasi email sebelum masuk.")
        raise ApiError(
            400 if register else 401,
            "REGISTRATION_FAILED" if register else "INVALID_CREDENTIALS",
            "Akun belum dapat dibuat. Periksa isian atau masuk jika sudah terdaftar."
            if register
            else "Email atau kata sandi tidak sesuai.",
        )
    try:
        result = upstream.json()
        token = result.get("access_token")
        if not token:
            if register:
                return AuthEnvelope(data=AuthData(status="verification_required"))
            raise ValueError
        session = AuthSession(
            access_token=token,
            refresh_token=result["refresh_token"],
            expires_in=result["expires_in"],
        )
    except (ValueError, KeyError, TypeError):
        raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons autentikasi tidak valid.") from None
    identity = await verify_access_token(request, token)
    database = request.app.state.database
    if database is None:
        return AuthEnvelope(data=AuthData(status="account_pending", session=session))
    try:
        # Run synchronous ORM work off the async event loop.
        from starlette.concurrency import run_in_threadpool

        account = await run_in_threadpool(
            bootstrap, database, request.app.state.settings, identity, True
        )
    except SQLAlchemyError:
        return AuthEnvelope(data=AuthData(status="account_pending", session=session))
    return AuthEnvelope(data=AuthData(status="authenticated", session=session, account=account))


@router.post(
    "/register",
    response_model=AuthEnvelope,
    status_code=201,
    summary="Create account with name, email and password",
    description=(
        "No bearer token required. Local Auth may activate immediately; hosted "
        "confirmation returns 202 without a session. Passwords are managed by Supabase Auth."
    ),
    responses={
        202: {"description": "Email verification required"},
        400: {"description": "Registration rejected"},
    },
)
async def register(body: RegisterRequest, request: Request, response: Response) -> AuthEnvelope:
    result = await password_action(request, body, register=True)
    if result.data.status == "verification_required":
        response.status_code = 202
    else:
        # Creating an account is not signing in. Retire the provider-issued
        # signup session and never return its tokens to clients.
        if result.data.session is not None:
            url, key = authority(request)
            try:
                await request.app.state.auth_http.post(
                    url + "/auth/v1/logout?scope=local",
                    headers={
                        "apikey": key,
                        "Authorization": "Bearer " + result.data.session.access_token,
                    },
                )
            except httpx2.HTTPError:
                # The account already exists. Tokens stay unexposed even if the
                # provider is temporarily unavailable for session cleanup.
                pass
        result.data.status = "registered"
        result.data.session = None
    return result


@router.post(
    "/login",
    response_model=AuthEnvelope,
    summary="Sign in with email and password",
    description=(
        "No bearer token required. Returns a session and prepares the Cashly account "
        "automatically. Use access_token in Swagger Authorize to call /me."
    ),
    responses={
        401: {"description": "Incorrect credentials"},
        403: {"description": "Email verification required"},
    },
)
async def login(body: LoginRequest, request: Request) -> AuthEnvelope:
    return await password_action(request, body, register=False)


@router.post(
    "/email/resend",
    response_model=AuthEnvelope,
    status_code=202,
    summary="Resend signup verification email",
    responses={
        429: {"description": "Provider send limit reached"},
        503: {"description": "Auth provider unavailable"},
    },
)
async def resend_email(body: EmailVerificationRequest, request: Request) -> AuthEnvelope:
    await email_provider_action(request, "/resend", {"email": body.email, "type": "signup"})
    # A generic result must not reveal whether an address has an account.
    return AuthEnvelope(data=AuthData(status="verification_required"))


@router.post(
    "/email/verify",
    response_model=AuthEnvelope,
    summary="Verify signup email OTP without signing in",
    responses={
        400: {"description": "Invalid or expired verification code"},
        429: {"description": "Provider attempt limit reached"},
        503: {"description": "Auth provider unavailable"},
    },
)
async def verify_email(body: VerifyEmailRequest, request: Request) -> AuthEnvelope:
    result = await email_provider_action(
        request,
        "/verify",
        {
            "email": body.email,
            "token": body.token.get_secret_value(),
            "type": "email",
        },
    )
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons autentikasi tidak valid.")
    try:
        await verify_access_token(request, token)
    finally:
        url, key = authority(request)
        try:
            await request.app.state.auth_http.post(
                url + "/auth/v1/logout?scope=local",
                headers={"apikey": key, "Authorization": "Bearer " + token},
            )
        except httpx2.HTTPError:
            pass
    # Cashly account initialization happens at the next explicit login.
    return AuthEnvelope(data=AuthData(status="registered"))


async def email_provider_action(request: Request, path: str, payload: dict) -> dict:
    url, key = authority(request)
    try:
        result = await request.app.state.auth_http.post(
            url + "/auth/v1" + path,
            headers={"apikey": key},
            json=payload,
        )
    except httpx2.HTTPError:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Layanan autentikasi belum tersedia.") from None
    if result.status_code == 429:
        raise ApiError(429, "AUTH_RATE_LIMITED", "Tunggu sebelum mencoba kembali.")
    if result.status_code >= 500:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Layanan autentikasi belum tersedia.")
    if not result.is_success:
        if path == "/resend":
            # Do not leak account existence through provider error details.
            return {}
        raise ApiError(400, "EMAIL_OTP_INVALID", "Kode tidak valid atau kedaluwarsa.")
    try:
        data = result.json()
        if not isinstance(data, dict):
            raise ValueError
        return data
    except ValueError:
        raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons autentikasi tidak valid.") from None
