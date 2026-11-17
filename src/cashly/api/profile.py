from fastapi import APIRouter, Header, Request
from sqlalchemy.exc import IntegrityError

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.schemas.account import AccountEnvelope
from cashly.schemas.profile import (
    EmailChangeInput,
    EmailChangeVerify,
    EmptyAction,
    PasswordChangeVerify,
    ProfileInput,
    SecurityChallengeEnvelope,
    SecurityResultEnvelope,
)
from cashly.services import account_security
from cashly.services.accounts import account_data
from cashly.services.finance import locked_account

router = APIRouter(prefix="/me", tags=["Profile and account security"])


@router.patch("/profile", response_model=AccountEnvelope)
def update_profile(body: ProfileInput, request: Request, identity: VerifiedIdentity):
    try:
        with database(request).transaction(auth_user_id=identity.user_id) as session:
            account, profile = locked_account(session, identity)
            profile.display_name = body.display_name
            profile.username = body.username
            session.flush()
            return AccountEnvelope(
                data=account_data(account, profile, identity, False, request.app.state.settings)
            )
    except IntegrityError:
        raise ApiError(
            409, "USERNAME_UNAVAILABLE", "Username sudah digunakan. Pilih yang lain."
        ) from None


@router.post(
    "/security/email/request",
    status_code=202,
    response_model=SecurityChallengeEnvelope,
    responses={
        409: {
            "description": (
                "EMAIL_ALREADY_REGISTERED: email already belongs to another account; no OTP sent"
            )
        }
    },
)
def request_email(
    body: EmailChangeInput,
    request: Request,
    identity: VerifiedIdentity,
    authorization: str = Header(),
):
    return account_security.request_change(
        database(request),
        request.app.state.settings,
        identity,
        authorization.split(" ", 1)[1],
        "email",
        body.email,
    )


@router.post("/security/email/verify", response_model=SecurityResultEnvelope)
def verify_email(body: EmailChangeVerify, request: Request, identity: VerifiedIdentity):
    return account_security.verify_change(
        database(request), request.app.state.settings, identity, body, "email"
    )


@router.post(
    "/security/password/request", status_code=202, response_model=SecurityChallengeEnvelope
)
def request_password(
    body: EmptyAction, request: Request, identity: VerifiedIdentity, authorization: str = Header()
):
    return account_security.request_change(
        database(request),
        request.app.state.settings,
        identity,
        authorization.split(" ", 1)[1],
        "password",
    )


@router.post("/security/password/verify", response_model=SecurityResultEnvelope)
def verify_password(body: PasswordChangeVerify, request: Request, identity: VerifiedIdentity):
    return account_security.verify_change(
        database(request), request.app.state.settings, identity, body, "password"
    )
