"""Authenticated changes; provider OTPs stay provider-owned and tokens never leave here."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx2
from sqlalchemy import select

from cashly.core.errors import ApiError
from cashly.db.models import AccountSecurityChallenge
from cashly.services.finance import locked_account


def matches_identity(user, user_id, email):
    try:
        return (
            UUID(user.get("id", "")) == user_id
            and str(user.get("email", "")).casefold() == email.casefold()
        )
    except (ValueError, TypeError, AttributeError):
        return False


def provider(client, settings, method, path, payload=None, token=None):
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise ApiError(503, "AUTH_NOT_CONFIGURED", "Supabase Auth belum dikonfigurasi.")
    headers = {"apikey": settings.supabase_publishable_key.get_secret_value()}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        response = client.request(
            method, settings.supabase_url + "/auth/v1" + path, headers=headers, json=payload
        )
    except httpx2.HTTPError:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Layanan akun belum tersedia.") from None
    if response.status_code == 429:
        raise ApiError(429, "AUTH_RATE_LIMITED", "Tunggu sebelum meminta atau mencoba kode lagi.")
    if response.status_code >= 500:
        raise ApiError(503, "AUTH_UNAVAILABLE", "Layanan akun belum tersedia.")
    if not response.is_success:
        if method == "PUT" and path == "/user" and payload and "email" in payload:
            try:
                detail = response.json()
                code = detail.get("error_code") or detail.get("code", "")
            except (ValueError, AttributeError):
                code = ""
            if code == "email_exists":
                raise ApiError(
                    409,
                    "EMAIL_ALREADY_REGISTERED",
                    "Email sudah terdaftar pada akun lain. Gunakan email yang berbeda.",
                )
        raise ApiError(
            400,
            "ACCOUNT_ACTION_REJECTED",
            "Permintaan ditolak. Periksa kode dan isian, atau minta kode baru.",
        )
    if response.status_code == 204:
        return {}
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons layanan akun tidak valid.") from None


def request_change(db, settings, identity, access_token, purpose, new_email=None):
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, _ = locked_account(session, identity)
        now = datetime.now(UTC)
        recent = session.scalar(
            select(AccountSecurityChallenge).where(
                AccountSecurityChallenge.account_id == account.id,
                AccountSecurityChallenge.expires_at > now + timedelta(minutes=4),
            )
        )
        if recent:
            raise ApiError(429, "AUTH_RATE_LIMITED", "Tunggu 60 detik sebelum meminta kode lagi.")
        if purpose == "email" and new_email.casefold() == identity.email.casefold():
            raise ApiError(422, "EMAIL_UNCHANGED", "Masukkan email yang berbeda.")
        with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            if purpose == "email":
                provider(client, settings, "PUT", "/user", {"email": new_email}, access_token)
            else:
                # Recovery OTP always proves mailbox possession, including a just-created
                # Google session. Native reauthentication skips nonce checks for fresh sessions.
                provider(client, settings, "POST", "/recover", {"email": identity.email})
        # A new request invalidates older app challenges as well as the provider's old codes.
        for old in session.scalars(
            select(AccountSecurityChallenge).where(
                AccountSecurityChallenge.account_id == account.id,
                AccountSecurityChallenge.purpose == purpose,
                AccountSecurityChallenge.consumed_at.is_(None),
            )
        ):
            old.consumed_at = now
        challenge = AccountSecurityChallenge(
            account_id=account.id,
            auth_user_id=identity.user_id,
            purpose=purpose,
            current_email=identity.email,
            new_email=new_email,
            expires_at=now + timedelta(minutes=5),
        )
        session.add(challenge)
        session.flush()
        return {
            "data": {
                "status": "verification_required",
                "challenge_id": str(challenge.id),
                "expires_at": challenge.expires_at.isoformat(),
                "current_email": identity.email,
                "new_email": new_email,
            }
        }


def verify_change(db, settings, identity, body, purpose):
    failure = None
    result = None
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = locked_account(session, identity)
        challenge = session.scalar(
            select(AccountSecurityChallenge)
            .where(
                AccountSecurityChallenge.id == body.challenge_id,
                AccountSecurityChallenge.account_id == account.id,
                AccountSecurityChallenge.auth_user_id == identity.user_id,
                AccountSecurityChallenge.purpose == purpose,
            )
            .with_for_update()
        )
        now = datetime.now(UTC)
        if not challenge or challenge.consumed_at or challenge.expires_at <= now:
            raise ApiError(
                409, "ACCOUNT_CHALLENGE_EXPIRED", "Kode sudah tidak berlaku. Minta kode baru."
            )
        if identity.email.casefold() != challenge.current_email.casefold():
            raise ApiError(
                409, "ACCOUNT_IDENTITY_CHANGED", "Email akun berubah. Mulai ulang permintaan."
            )
        if challenge.attempts >= 8:
            raise ApiError(429, "AUTH_RATE_LIMITED", "Batas percobaan tercapai. Minta kode baru.")
        challenge.attempts += 1
        with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            temporary = None
            try:
                address = getattr(body, "address", None)
                email = challenge.new_email if address == "new" else challenge.current_email
                response = provider(
                    client,
                    settings,
                    "POST",
                    "/verify",
                    {
                        "email": email,
                        "token": body.token.get_secret_value(),
                        "type": "email_change" if purpose == "email" else "recovery",
                    },
                )
                temporary = response.get("access_token")
                if purpose == "email":
                    if address == "new":
                        challenge.confirmed_new = True
                    else:
                        challenge.confirmed_current = True
                    if temporary:
                        user = provider(client, settings, "GET", "/user", token=temporary)
                        if not matches_identity(user, identity.user_id, challenge.new_email):
                            raise ApiError(
                                403,
                                "ACCOUNT_IDENTITY_MISMATCH",
                                "Identitas verifikasi tidak sesuai.",
                            )
                        if not (challenge.confirmed_current and challenge.confirmed_new):
                            raise ApiError(
                                503,
                                "SECURE_EMAIL_CHANGE_REQUIRED",
                                "Aktifkan Secure Email Change di Supabase.",
                            )
                        challenge.consumed_at = now
                        result = {"data": {"status": "email_changed"}}
                    else:
                        result = {
                            "data": {
                                "status": "confirmation_pending",
                                "confirmed_current": challenge.confirmed_current,
                                "confirmed_new": challenge.confirmed_new,
                            }
                        }
                else:
                    if not temporary:
                        raise ApiError(
                            503,
                            "AUTH_INVALID_RESPONSE",
                            "Verifikasi tidak menghasilkan sesi pemulihan.",
                        )
                    user = provider(client, settings, "GET", "/user", token=temporary)
                    if not matches_identity(user, identity.user_id, challenge.current_email):
                        raise ApiError(
                            403, "ACCOUNT_IDENTITY_MISMATCH", "Identitas verifikasi tidak sesuai."
                        )
                    challenge.consumed_at = now
                    provider(
                        client,
                        settings,
                        "PUT",
                        "/user",
                        {"password": body.password.get_secret_value()},
                        temporary,
                    )
                    profile.password_login_enabled = True
                    result = {"data": {"status": "password_changed"}}
            except ApiError as error:
                failure = error
            finally:
                if temporary:
                    try:
                        provider(client, settings, "POST", "/logout?scope=local", token=temporary)
                    except ApiError:
                        pass
    if failure:
        raise failure
    return result
