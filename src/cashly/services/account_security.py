"""Account security: current credentials and single-use email proofs stay server-side."""

from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx2

from cashly.core.errors import ApiError
from cashly.db.models import Profile
from cashly.repositories.accounts import find_link, set_account_context
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
        if method == "PUT" and path.split("?", 1)[0] == "/user" and payload:
            try:
                detail = response.json()
                code = detail.get("error_code") or detail.get("code", "")
            except (ValueError, AttributeError):
                code = ""
            if code == "same_password" and "password" in payload:
                raise ApiError(
                    422, "PASSWORD_UNCHANGED", "Kata sandi baru harus berbeda dari sebelumnya."
                )
        raise ApiError(
            400,
            "ACCOUNT_ACTION_REJECTED",
            "Permintaan ditolak. Periksa isian atau minta tautan baru.",
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


def google_connection(db, settings, identity, access_token, *, disconnect=False):
    with db.transaction(auth_user_id=identity.user_id) as session:
        _, profile = locked_account(session, identity)
        with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            user = provider(client, settings, "GET", "/user", token=access_token)
            if not matches_identity(user, identity.user_id, identity.email):
                raise ApiError(
                    403, "ACCOUNT_IDENTITY_MISMATCH", "Identitas akun berubah. Masuk kembali."
                )
            identities = user.get("identities")
            if not isinstance(identities, list) or any(not isinstance(i, dict) for i in identities):
                raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons layanan akun tidak valid.")
            google = next((i for i in identities if i.get("provider") == "google"), None)
            email_ready = bool(user.get("email_confirmed_at")) and any(
                i.get("provider") == "email" for i in identities
            )
            allowed = bool(google and profile.password_login_enabled and email_ready)
            if disconnect and google:
                if not profile.password_login_enabled:
                    raise ApiError(
                        409,
                        "GOOGLE_PASSWORD_REQUIRED",
                        "Buat kata sandi Cashly sebelum melepas Google.",
                    )
                if not email_ready:
                    raise ApiError(
                        409,
                        "GOOGLE_EMAIL_LOGIN_REQUIRED",
                        "Metode login email belum siap. Google tetap terhubung.",
                    )
                try:
                    identifier = str(UUID(google.get("identity_id", "")))
                except (ValueError, TypeError, AttributeError):
                    raise ApiError(
                        503, "AUTH_INVALID_RESPONSE", "Identitas Google belum dapat diperiksa."
                    ) from None
                provider(
                    client, settings, "DELETE", "/user/identities/" + identifier, token=access_token
                )
                google = None
                allowed = False
            details = (google.get("identity_data") or {}) if google else {}
            email = details.get("email") if isinstance(details, dict) else None
            return {
                "data": {
                    "connected": google is not None,
                    "google_email": email if isinstance(email, str) else None,
                    "password_login_enabled": profile.password_login_enabled,
                    "can_disconnect": allowed,
                }
            }


def validate_current_password(client, settings, identity, current_password, password=None):
    if current_password is None:
        raise ApiError(422, "CURRENT_PASSWORD_REQUIRED", "Masukkan kata sandi saat ini.")
    try:
        result = provider(
            client,
            settings,
            "POST",
            "/token?grant_type=password",
            {"email": identity.email, "password": current_password.get_secret_value()},
        )
    except ApiError as error:
        if error.status_code == 400:
            raise ApiError(
                400, "CURRENT_PASSWORD_INVALID", "Kata sandi saat ini tidak sesuai."
            ) from None
        raise
    token = result.get("access_token")
    if not isinstance(token, str) or not token:
        raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons layanan akun tidak valid.")
    try:
        user = provider(client, settings, "GET", "/user", token=token)
        if not matches_identity(user, identity.user_id, identity.email):
            raise ApiError(403, "ACCOUNT_IDENTITY_MISMATCH", "Identitas akun tidak sesuai.")
        if password is not None:
            if current_password.get_secret_value() == password.get_secret_value():
                raise ApiError(
                    422, "PASSWORD_UNCHANGED", "Kata sandi baru harus berbeda dari sebelumnya."
                )
            # Use the fresh credential proof, so a long-lived browser session
            # cannot skip current-password validation or require an email nonce.
            provider(
                client, settings, "PUT", "/user", {"password": password.get_secret_value()}, token
            )
    finally:
        try:
            provider(client, settings, "POST", "/logout?scope=local", token=token)
        except ApiError:
            pass


def trusted_redirect(settings, value, path):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ApiError(422, "INVALID_AUTH_REDIRECT", "URL konfirmasi belum diizinkan.") from None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if (
        origin not in settings.cors_origins
        or parsed.path != path
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ApiError(422, "INVALID_AUTH_REDIRECT", "URL konfirmasi belum diizinkan.")
    return value


def change_password(db, settings, identity, body, access_token):
    with db.transaction(auth_user_id=identity.user_id) as session:
        _, profile = locked_account(session, identity)
        if not profile.password_login_enabled:
            raise ApiError(
                409,
                "PASSWORD_SETUP_REQUIRED",
                "Buat kata sandi melalui tautan email terlebih dahulu.",
            )
        with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            validate_current_password(
                client, settings, identity, body.current_password, body.password
            )
        return {"data": {"status": "password_changed"}}


def request_setup(db, settings, identity, body):
    redirect = trusted_redirect(settings, body.redirect_to, "/auth/password/callback")
    with db.transaction(auth_user_id=identity.user_id) as session:
        _, profile = locked_account(session, identity)
        if profile.password_login_enabled:
            raise ApiError(409, "PASSWORD_ALREADY_SET", "Gunakan menu ganti kata sandi.")
        with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            provider(
                client,
                settings,
                "POST",
                "/recover?" + urlencode({"redirect_to": redirect}),
                {"email": identity.email},
            )
    return {"data": {"status": "link_sent"}}


def recovery_user(client, settings, token):
    user = provider(client, settings, "GET", "/user", token=token)
    try:
        user_id = UUID(user["id"])
        email = user["email"]
        if not isinstance(email, str) or not email or not user.get("email_confirmed_at"):
            raise ValueError
        return user_id, email
    except (KeyError, TypeError, ValueError):
        raise ApiError(
            400, "RECOVERY_IDENTITY_MISMATCH", "Identitas pemulihan tidak sesuai."
        ) from None


def close_recovery(client, settings, token):
    try:
        provider(client, settings, "POST", "/logout?scope=local", token=token)
    except ApiError:
        pass


def start_recovery(db, settings, body):
    from cashly.repositories import password_recovery

    with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        response = provider(
            client,
            settings,
            "POST",
            "/verify",
            {"token_hash": body.token_hash.get_secret_value(), "type": "recovery"},
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise ApiError(503, "AUTH_INVALID_RESPONSE", "Respons layanan akun tidak valid.")
        try:
            user_id, email = recovery_user(client, settings, token)
            if email.casefold() != body.email.casefold():
                raise ApiError(
                    400, "RECOVERY_IDENTITY_MISMATCH", "Tautan pemulihan tidak sesuai dengan email."
                )
            row = password_recovery.create(db, user_id, email, token)
            return {
                "data": {
                    "status": "recovery_ready",
                    "recovery_id": row.id,
                    "recovery_token": token,
                    "expires_at": row.expires_at,
                    "email": email,
                }
            }
        except Exception:
            close_recovery(client, settings, token)
            raise


def reset_password(settings, body, db):
    from cashly.repositories import password_recovery

    token = body.recovery_token.get_secret_value()
    keep = False
    reserved = False
    with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        try:
            user_id, email = recovery_user(client, settings, token)
            row = password_recovery.reserve(db, user_id, body.recovery_id, token)
            reserved = True
            if email.casefold() != row.email.casefold():
                raise ApiError(409, "RECOVERY_EXPIRED", "Email akun berubah. Minta tautan baru.")
            try:
                provider(
                    client,
                    settings,
                    "PUT",
                    "/user",
                    {"password": body.password.get_secret_value()},
                    token,
                )
            except ApiError as error:
                if error.status_code in {400, 422}:
                    # An explicit provider validation failure made no password write.
                    # Preserve the original expiry, never extend it on retry.
                    keep = password_recovery.reopen_after_rejection(db, row)
                raise
            with db.transaction(auth_user_id=user_id) as session:
                link = find_link(session, user_id)
                if link is not None:
                    set_account_context(session, link.account_id)
                    profile = session.get(Profile, link.account_id, with_for_update=True)
                    if profile is not None:
                        profile.password_login_enabled = True
            return {"data": {"status": "password_reset"}}
        finally:
            if reserved and not keep:
                close_recovery(client, settings, token)


def cancel_recovery(db, settings, body):
    from cashly.repositories import password_recovery

    token = body.recovery_token.get_secret_value()
    reserved = False
    with httpx2.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        try:
            user_id, _ = recovery_user(client, settings, token)
            password_recovery.reserve(db, user_id, body.recovery_id, token)
            reserved = True
        finally:
            if reserved:
                close_recovery(client, settings, token)
    return {"data": {"status": "recovery_cancelled"}}
