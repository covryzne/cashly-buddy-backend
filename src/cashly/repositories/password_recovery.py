"""Only hashed proof and lifecycle state are persisted, never provider tokens."""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, update

from cashly.core.errors import ApiError
from cashly.db.models import PasswordRecoverySession
from cashly.repositories.accounts import lock_identity


def digest(token):
    return sha256(token.encode()).hexdigest()


def create(db, user_id, email, token):
    now = datetime.now(UTC)
    row = PasswordRecoverySession(
        id=uuid4(),
        auth_user_id=user_id,
        email=email,
        token_digest=digest(token),
        expires_at=now + timedelta(minutes=5),
    )
    with db.transaction(auth_user_id=user_id) as session:
        lock_identity(session, user_id)
        session.execute(
            update(PasswordRecoverySession)
            .where(
                PasswordRecoverySession.auth_user_id == user_id,
                PasswordRecoverySession.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )
        session.add(row)
    return row


def reserve(db, user_id, recovery_id, token):
    with db.transaction(auth_user_id=user_id) as session:
        row = session.scalar(
            select(PasswordRecoverySession)
            .where(
                PasswordRecoverySession.id == recovery_id,
                PasswordRecoverySession.auth_user_id == user_id,
                PasswordRecoverySession.token_digest == digest(token),
            )
            .with_for_update()
        )
        if row is None or row.consumed_at or row.expires_at <= datetime.now(UTC):
            raise ApiError(
                409, "RECOVERY_EXPIRED", "Sesi pemulihan sudah berakhir. Minta tautan baru."
            )
        # Commit reservation before the external write. Concurrent or replayed
        # requests cannot change the password twice, even on an ambiguous timeout.
        row.consumed_at = datetime.now(UTC)
    return row


def reopen_after_rejection(db, row):
    with db.transaction(auth_user_id=row.auth_user_id) as session:
        lock_identity(session, row.auth_user_id)
        current = session.get(PasswordRecoverySession, row.id, with_for_update=True)
        another = session.scalar(
            select(PasswordRecoverySession.id).where(
                PasswordRecoverySession.auth_user_id == row.auth_user_id,
                PasswordRecoverySession.id != row.id,
                PasswordRecoverySession.consumed_at.is_(None),
            )
        )
        if current and current.expires_at > datetime.now(UTC) and another is None:
            current.consumed_at = None
            return True
    return False


def is_recovery_token(db, user_id, token):
    with db.transaction(auth_user_id=user_id) as session:
        return (
            session.scalar(
                select(PasswordRecoverySession.id).where(
                    PasswordRecoverySession.auth_user_id == user_id,
                    PasswordRecoverySession.token_digest == digest(token),
                )
            )
            is not None
        )
