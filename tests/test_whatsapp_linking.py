import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_postgres_settings as auth_postgres_settings
from test_auth import prepare, runtime_database

from cashly.core.auth import Identity, require_identity
from cashly.core.settings import Settings
from cashly.db.connection import build_engine
from cashly.main import create_app
from cashly.services import whatsapp
from cashly.services.accounts import bootstrap


@pytest.fixture
def wa(auth_postgres_settings, monkeypatch):
    now = [datetime.now(UTC)]
    monkeypatch.setattr(whatsapp, "clock", lambda: now[0])
    identities = [
        Identity(uuid4(), f"user{i}@example.invalid", f"User {i}", now[0]) for i in range(2)
    ]
    settings = auth_postgres_settings.model_copy(
        update={
            "whatsapp_enabled": True,
            "whatsapp_bot_number": "628111111110",
            "whatsapp_integration_key": SecretStr("integration-test-" + "x" * 40),
            "whatsapp_hash_secret": SecretStr("hash-test-" + "y" * 40),
            "whatsapp_encryption_key": SecretStr(Fernet.generate_key().decode()),
            "db_pool_size": 4,
        }
    )
    prepare(settings, identities)
    with TestClient(create_app(settings)) as api:
        original = api.app.state.database
        db = runtime_database(settings)
        api.app.state.database = db
        accounts = [bootstrap(db, settings, identity) for identity in identities]

        api.app.dependency_overrides[require_identity] = lambda: identities[0]
        try:
            yield api, settings, db, now, identities, accounts
        finally:
            api.app.state.database = original
            db.dispose()


def headers(settings):
    return {"X-Cashly-Integration-Key": settings.whatsapp_integration_key.get_secret_value()}


def challenge(api, key=None):
    result = api.post(
        "/api/v1/me/whatsapp/challenges", headers={"Idempotency-Key": key or str(uuid4())}
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def incoming(challenge, now, *, sender="628111111111@s.whatsapp.net", message=None, mid=None):
    return {
        "contract_version": "cashly-wa-v1",
        "environment": "dev",
        "provider": "evolution",
        "bot_id": "cashly-staging",
        "event_type": "messages.upsert",
        "message_id": mid or uuid4().hex,
        "sender_id": sender,
        "sent_at": now.isoformat(),
        "received_at": now.isoformat(),
        "kind": "text",
        "text": message
        if message is not None
        else parse_qs(urlsplit(challenge["chat_url"]).query)["text"][0],
    }


def event(api, settings, body):
    result = api.post("/api/v1/integrations/whatsapp/events", json=body, headers=headers(settings))
    assert result.status_code == 200, result.text
    return result.json()["data"]


def claim(api, settings, reply_id=None):
    result = api.post(
        "/api/v1/integrations/whatsapp/outbox/claim",
        json={"reply_id": reply_id} if reply_id else {},
        headers=headers(settings),
    )
    assert result.status_code == 200, result.text
    return result.json()["data"]


def verify(api, row, code):
    return api.post(f"/api/v1/me/whatsapp/challenges/{row['id']}/verify", json={"code": code})


def test_linking_roundtrip_owned_and_encrypted(wa):
    api, settings, db, now, identities, accounts = wa
    key = str(uuid4())
    row = challenge(api, key)
    assert challenge(api, key)["id"] == row["id"]
    result = event(api, settings, incoming(row, now[0]))
    code = re.search(r"\b[0-9]{6}\b", result["reply"]["text"])[0]
    assert "code" not in row and "otp" not in row
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "not_linked"
    admin = build_engine(settings, migration=True)
    with admin.connect() as conn:
        cipher = conn.execute(text("SELECT payload_cipher FROM cashly.whatsapp_outbox")).scalar()
        assert code not in cipher and "628111111111" not in cipher
    admin.dispose()
    claimed = claim(api, settings, result["reply"]["id"])
    assert claimed["reply"]["lease_id"]
    assert claim(api, settings, result["reply"]["id"]) is None
    report = {
        "contract_version": "cashly-wa-v1",
        "environment": "dev",
        "provider": "evolution",
        "bot_id": "cashly-staging",
        "event_id": result["event_id"],
        "reply_id": result["reply"]["id"],
        "lease_id": claimed["reply"]["lease_id"],
        "recipient_id": result["sender_id"],
        "status": "accepted",
        "outbound_message_id": "SYNTHETIC-OUTBOUND",
    }
    for _ in range(2):
        assert (
            api.post(
                "/api/v1/integrations/whatsapp/deliveries", json=report, headers=headers(settings)
            ).status_code
            == 200
        )
    api.app.dependency_overrides[require_identity] = lambda: identities[1]
    assert verify(api, row, code).status_code == 404
    api.app.dependency_overrides[require_identity] = lambda: identities[0]
    linked = verify(api, row, code)
    assert linked.status_code == 200, linked.text
    assert linked.json()["data"]["status"] == "connected"
    assert linked.json()["data"]["masked_number"] == "•••• 1111"
    assert verify(api, row, code).status_code == 409
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "connected"
    for kind in ("text", "receipt", "voice"):
        reply = event(api, settings, incoming(row, now[0], message="kopi 18rb") | {"kind": kind})
        assert reply["verified_link"] and reply["action"] == "reply"
        assert "masih disiapkan" in reply["reply"]["text"]
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "not_linked"
    assert (
        api.post(
            "/api/v1/integrations/whatsapp/deliveries", json=report, headers=headers(settings)
        ).status_code
        == 200
    )
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "not_linked"


def test_wrong_code_attempts_commit_and_resend_invalidates_old(wa):
    api, settings, db, now, _, __ = wa
    row = challenge(api)
    reply = event(api, settings, incoming(row, now[0]))
    code = re.search(r"\b[0-9]{6}\b", reply["reply"]["text"])[0]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for i in range(5):
        result = verify(api, row, wrong)
        assert result.status_code == (400 if i < 4 else 429), result.text
    assert verify(api, row, code).status_code == 409
    assert (
        api.post(
            "/api/v1/me/whatsapp/challenges", headers={"Idempotency-Key": str(uuid4())}
        ).status_code
        == 429
    )
    now[0] += timedelta(seconds=61)
    new = challenge(api)
    assert new["id"] != row["id"]
    assert verify(api, row, code).status_code == 409
    reply = event(api, settings, incoming(new, now[0]))
    new_code = re.search(r"\b[0-9]{6}\b", reply["reply"]["text"])[0]
    assert verify(api, new, new_code).status_code == 200


def test_replay_claim_race_and_lease_expiry(wa):
    api, settings, db, now, _, __ = wa
    row = challenge(api)
    body = incoming(row, now[0])
    first = event(api, settings, body)
    replay = event(
        api, settings, {**body, "received_at": (now[0] + timedelta(seconds=1)).isoformat()}
    )
    assert first["reply"]["id"] == replay["reply"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(api, settings, first["reply"]["id"]), range(2)))
    assert sum(item is not None for item in claims) == 1
    now[0] += timedelta(minutes=4)
    assert claim(api, settings) is None
    assert event(api, settings, body)["action"] == "ignore"
    changed = api.post(
        "/api/v1/integrations/whatsapp/events",
        json={**body, "text": "different"},
        headers=headers(settings),
    )
    assert changed.status_code == 409


def test_expiry_lid_and_conflict(wa):
    api, settings, db, now, identities, _ = wa
    row = challenge(api)
    body = incoming(row, now[0], sender="123456789@lid")
    missing_phone = event(api, settings, body)
    assert not re.search(r"\b[0-9]{6}\b", missing_phone["reply"]["text"])
    body.update(message_id=uuid4().hex, sender_phone_jid="628111111111@s.whatsapp.net")
    result = event(api, settings, body)
    code = re.search(r"\b[0-9]{6}\b", result["reply"]["text"])[0]
    assert verify(api, row, code).status_code == 200
    api.app.dependency_overrides[require_identity] = lambda: identities[1]
    row2 = challenge(api)
    conflict = event(api, settings, incoming(row2, now[0]))
    assert "tidak dapat dihubungkan" in conflict["reply"]["text"]
    now[0] += timedelta(minutes=6)
    assert verify(api, row2, "123456").status_code == 409


def test_integration_auth_scope_rate_and_no_finance(wa):
    api, settings, db, now, _, __ = wa
    row = challenge(api)
    body = incoming(row, now[0], message="kopi 18rb")
    assert api.post("/api/v1/integrations/whatsapp/events", json=body).status_code == 401
    assert (
        api.post(
            "/api/v1/integrations/whatsapp/events",
            json={**body, "bot_id": "other"},
            headers=headers(settings),
        ).status_code
        == 403
    )
    for kind in ("text", "receipt", "voice"):
        result = event(api, settings, {**body, "kind": kind, "message_id": uuid4().hex})
        assert result["action"] == "reply" and not result["verified_link"]
    with db.transaction(auth_user_id=wa[4][0].user_id) as session:
        assert session.execute(text("SELECT count(*) FROM cashly.expenses")).scalar() == 0
    for _ in range(7):
        event(api, settings, {**body, "message_id": uuid4().hex})
    assert (
        api.post(
            "/api/v1/integrations/whatsapp/events",
            json={**body, "message_id": uuid4().hex},
            headers=headers(settings),
        ).status_code
        == 429
    )


def test_disconnect_cancels_pending_and_account_delete_cascades(wa):
    api, settings, db, now, identities, _ = wa
    row = challenge(api)
    result = event(api, settings, incoming(row, now[0]))
    code = re.search(r"\b[0-9]{6}\b", result["reply"]["text"])[0]
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    assert claim(api, settings, result["reply"]["id"]) is None
    assert verify(api, row, code).status_code == 409
    admin = build_engine(settings, migration=True)
    with admin.begin() as conn:
        conn.execute(text("DELETE FROM auth.users WHERE id=:id"), {"id": identities[0].user_id})
        assert conn.execute(text("SELECT count(*) FROM cashly.whatsapp_challenges")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM cashly.whatsapp_outbox")).scalar() == 0
    admin.dispose()


def test_same_phone_verification_race_and_rls_isolation(wa):
    api, settings, db, now, identities, accounts = wa
    first = challenge(api)
    one = event(api, settings, incoming(first, now[0]))
    code1 = re.search(r"\b[0-9]{6}\b", one["reply"]["text"])[0]
    api.app.dependency_overrides[require_identity] = lambda: identities[1]
    second = challenge(api)
    two = event(api, settings, incoming(second, now[0]))
    code2 = re.search(r"\b[0-9]{6}\b", two["reply"]["text"])[0]

    # Separate callers use the service directly to avoid racing a global test override.
    def attempt(args):
        identity, row, code = args
        try:
            return whatsapp.verify(db, settings, identity, row["id"], code)["data"]["status"]
        except Exception as failure:
            return getattr(failure, "status_code", None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(attempt, [(identities[0], first, code1), (identities[1], second, code2)])
        )
    assert outcomes.count("connected") == 1, outcomes
    assert outcomes.count(409) == 1, outcomes
    assert claim(api, settings) is None
    with db.transaction() as session:
        assert (
            session.execute(text("SELECT count(*) FROM cashly.whatsapp_challenges")).scalar() == 0
        )
        assert session.execute(text("SELECT count(*) FROM cashly.whatsapp_channels")).scalar() == 0
    for account, identity in zip(accounts, identities, strict=True):
        with db.transaction(auth_user_id=identity.user_id) as session:
            session.execute(
                text("SELECT set_config('cashly.account_id',:id,true)"),
                {"id": str(account.account_id)},
            )
            owners = (
                session.execute(text("SELECT account_id FROM cashly.whatsapp_challenges"))
                .scalars()
                .all()
            )
            assert owners and set(owners) == {account.account_id}
    with db.transaction() as session:
        assert (
            session.execute(
                text("SELECT current_setting('cashly.whatsapp_integration',true)")
            ).scalar()
            != "on"
        )


def test_delivery_rejects_wrong_lease_and_recipient(wa):
    api, settings, _, now, _, _ = wa
    row = challenge(api)
    reply = event(api, settings, incoming(row, now[0]))
    claimed = claim(api, settings, reply["reply"]["id"])
    report = {
        "contract_version": "cashly-wa-v1",
        "environment": "dev",
        "provider": "evolution",
        "bot_id": "cashly-staging",
        "event_id": reply["event_id"],
        "reply_id": reply["reply"]["id"],
        "lease_id": claimed["reply"]["lease_id"],
        "recipient_id": reply["sender_id"],
        "status": "accepted",
        "outbound_message_id": "SYNTHETIC",
    }
    for changed in ({"lease_id": str(uuid4())}, {"recipient_id": "628111111112@s.whatsapp.net"}):
        assert (
            api.post(
                "/api/v1/integrations/whatsapp/deliveries",
                json={**report, **changed},
                headers=headers(settings),
            ).status_code
            == 409
        )
    assert (
        api.post(
            "/api/v1/integrations/whatsapp/deliveries", json=report, headers=headers(settings)
        ).status_code
        == 200
    )
    assert claim(api, settings) is None


def test_user_routes_require_login_and_disabled_configuration_fails_closed():
    with TestClient(create_app(Settings(environment="dev"))) as api:
        for method, path in (("GET", ""), ("DELETE", ""), ("POST", "/challenges")):
            assert api.request(method, "/api/v1/me/whatsapp" + path).status_code == 401
        assert api.post("/api/v1/integrations/whatsapp/outbox/claim", json={}).status_code == 503
