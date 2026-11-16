"""Validate bearer tokens against the configured Supabase Auth authority."""

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated
from uuid import UUID

import httpx2
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cashly.core.errors import ApiError


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    email: str
    display_name: str
    verified_at: datetime


async def require_identity(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))
    ],
) -> Identity:
    token = credentials.credentials if credentials else ""
    return await verify_access_token(request, token)


async def verify_access_token(request: Request, token: str) -> Identity:
    if not token or len(token) > 16384:
        raise ApiError(
            401,
            "UNAUTHENTICATED",
            "Access token diperlukan. Login terlebih dahulu, lalu gunakan Authorize di Swagger.",
        )
    settings = request.app.state.settings
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise HTTPException(503)
    try:
        # getUser validates this access token at the Auth authority. Never decode an
        # unverified JWT and use its `sub`, roles, email or account ID as authority.
        response = await request.app.state.auth_http.get(
            settings.supabase_url.rstrip("/") + "/auth/v1/user",
            headers={
                "apikey": settings.supabase_publishable_key.get_secret_value(),
                "Authorization": f"Bearer {token}",
            },
        )
    except httpx2.HTTPError:
        raise HTTPException(503) from None
    if response.status_code in {401, 403}:
        raise HTTPException(401)
    if response.status_code != 200:
        raise HTTPException(503)
    try:
        user = response.json()
        verified = user.get("email_confirmed_at")
        if not verified or not user.get("email"):
            raise HTTPException(403)
        providers = {i.get("provider") for i in user.get("identities", [])}
        if not providers.intersection({"google", "email"}):
            raise HTTPException(403)
        metadata = user.get("user_metadata") or {}
        name = metadata.get("full_name") or metadata.get("name") or ""
        name = name.strip() if isinstance(name, str) else ""
        if not 2 <= len(name) <= 40 or any(ord(char) < 32 for char in name):
            name = "Pengguna Cashly"
        return Identity(
            user_id=UUID(user["id"]),
            email=user["email"],
            display_name=name,
            verified_at=datetime.fromisoformat(verified.replace("Z", "+00:00")),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(503) from None
