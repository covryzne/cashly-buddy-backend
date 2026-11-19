from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from cashly.core.auth import Identity, require_identity
from cashly.db.connection import Database
from cashly.schemas.account import AccountEnvelope, ExpensesEnvelope
from cashly.services import accounts

router = APIRouter(tags=["Account"])
VerifiedIdentity = Annotated[Identity, Depends(require_identity)]


def database(request: Request) -> Database:
    db = request.app.state.database
    if db is None:
        raise HTTPException(503)
    return db


@router.post(
    "/auth/bootstrap",
    response_model=AccountEnvelope,
    summary="Retry account preparation after successful authentication",
    description=(
        "Internal/retry operation. Requires bearer access_token via Authorize. "
        "No body: identity comes from the verified token. "
        "Use /auth/register or /auth/login for password authentication."
    ),
)
def bootstrap(request: Request, response: Response, identity: VerifiedIdentity) -> AccountEnvelope:
    response.headers["Cache-Control"] = "private, no-store"
    return AccountEnvelope(
        data=accounts.bootstrap(database(request), request.app.state.settings, identity)
    )


@router.get("/me", response_model=AccountEnvelope)
def me(request: Request, response: Response, identity: VerifiedIdentity) -> AccountEnvelope:
    response.headers["Cache-Control"] = "private, no-store"
    return AccountEnvelope(
        data=accounts.me(database(request), request.app.state.settings, identity)
    )


def expenses(request: Request, response: Response, identity: VerifiedIdentity) -> ExpensesEnvelope:
    response.headers["Cache-Control"] = "private, no-store"
    return accounts.expenses(database(request), identity)
