from fastapi import APIRouter, Header, Request
from sqlalchemy.exc import IntegrityError

from cashly.api.accounts import VerifiedIdentity, database
from cashly.core.errors import ApiError
from cashly.schemas.account import AccountEnvelope
from cashly.schemas.profile import (
    AccountDeletionEnvelope,
    AccountDeletionInput,
    EmptyAction,
    GoogleConnectionEnvelope,
    PasswordSetupRequest,
    PasswordUpdate,
    ProfileInput,
    SecurityResultEnvelope,
)
from cashly.services import account_security
from cashly.services.accounts import account_data
from cashly.services.finance import locked_account

router = APIRouter(prefix="/me", tags=["Profile and account security"])


@router.delete("/account", response_model=AccountDeletionEnvelope)
def delete_account(body: AccountDeletionInput, request: Request, identity: VerifiedIdentity):
    from cashly.services.account_deletion import delete_account as perform_deletion

    return perform_deletion(database(request), request.app.state.settings, identity, body)


@router.get("/security/google", response_model=GoogleConnectionEnvelope)
def google_details(request: Request, identity: VerifiedIdentity, authorization: str = Header()):
    return account_security.google_connection(
        database(request), request.app.state.settings, identity, authorization.split(" ", 1)[1]
    )


@router.post(
    "/security/google/disconnect",
    response_model=GoogleConnectionEnvelope,
    responses={409: {"description": "Cashly password and remaining email identity required"}},
)
def disconnect_google(
    body: EmptyAction, request: Request, identity: VerifiedIdentity, authorization: str = Header()
):
    return account_security.google_connection(
        database(request),
        request.app.state.settings,
        identity,
        authorization.split(" ", 1)[1],
        disconnect=True,
    )


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
    "/security/password/setup/request", status_code=202, response_model=SecurityResultEnvelope
)
def setup_password(body: PasswordSetupRequest, request: Request, identity: VerifiedIdentity):
    return account_security.request_setup(
        database(request), request.app.state.settings, identity, body
    )


@router.post("/security/password/change", response_model=SecurityResultEnvelope)
def change_password(
    body: PasswordUpdate,
    request: Request,
    identity: VerifiedIdentity,
    authorization: str = Header(),
):
    return account_security.change_password(
        database(request),
        request.app.state.settings,
        identity,
        body,
        authorization.split(" ", 1)[1],
    )
