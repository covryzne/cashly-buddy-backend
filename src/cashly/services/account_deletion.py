"""Delete the caller via Auth admin API; DB cascade owns atomic data cleanup."""

import base64
import json

import httpx2
from sqlalchemy import select

from cashly.core.errors import ApiError
from cashly.db.models import AccountAuthLink
from cashly.services.account_security import validate_current_password
from cashly.services.finance import locked_account


def admin_key(settings):
    if not settings.supabase_secret_key or not settings.supabase_url:
        raise ApiError(
            503, "ACCOUNT_DELETION_NOT_CONFIGURED", "Hapus akun belum dikonfigurasi pada backend."
        )
    secret = settings.supabase_secret_key.get_secret_value()
    valid = secret.startswith("sb_secret_")
    if not valid:
        try:
            part = secret.split(".")[1]
            valid = (
                json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("role")
                == "service_role"
            )
        except (IndexError, ValueError, TypeError, AttributeError):
            valid = False
    if not valid:
        raise ApiError(
            503, "ACCOUNT_DELETION_NOT_CONFIGURED", "Secret Supabase backend belum sesuai."
        )
    return secret


def delete_account(db, settings, identity, body):
    secret = admin_key(settings)
    # Never hold a SQL row lock while Auth's delete transaction invokes the
    # cascade. A failure at Auth leaves all application data untouched.
    with db.transaction(auth_user_id=identity.user_id) as session:
        account, profile = locked_account(session, identity)
        links = session.scalars(
            select(AccountAuthLink.auth_user_id).where(AccountAuthLink.account_id == account.id)
        ).all()
        if links != [identity.user_id]:
            raise ApiError(
                409, "ACCOUNT_HAS_OTHER_OWNERS", "Akun ini belum dapat dihapus melalui aplikasi."
            )
        password_required = profile.password_login_enabled
    with httpx2.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        if password_required:
            validate_current_password(client, settings, identity, body.current_password)
        try:
            response = client.request(
                "DELETE",
                settings.supabase_url + "/auth/v1/admin/users/" + str(identity.user_id),
                headers={"apikey": secret, "Authorization": "Bearer " + secret},
                json={"should_soft_delete": False},
            )
        except httpx2.HTTPError:
            raise ApiError(
                503,
                "ACCOUNT_DELETION_UNAVAILABLE",
                "Penghapusan belum dapat dipastikan. Coba masuk kembali untuk memeriksa akun.",
            ) from None
        if response.status_code == 429:
            raise ApiError(429, "AUTH_RATE_LIMITED", "Tunggu sebentar sebelum mencoba lagi.")
        if not response.is_success:
            raise ApiError(
                503, "ACCOUNT_DELETION_UNAVAILABLE", "Akun belum dapat dihapus. Coba lagi."
            )
    return {"data": {"status": "account_deleted"}}
