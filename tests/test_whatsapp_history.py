import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import event as sql_event
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth import auth_postgres_settings as auth_postgres_settings
from test_auth import prepare
from test_whatsapp_linking import challenge, event, incoming, verify
from test_whatsapp_linking import wa as wa

from cashly.core.auth import require_identity
from cashly.db.commands import alembic_config
from cashly.db.connection import build_engine
from cashly.db.whatsapp_models import WhatsAppLinkHistory
from cashly.services import whatsapp


def history(api, **params):
    response = api.get("/api/v1/me/whatsapp/history", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def connect(wa, sender="628111111111@s.whatsapp.net"):
    api, settings, _, now, _, _ = wa
    row = challenge(api)
    result = event(api, settings, incoming(row, now[0], sender=sender))
    code = re.search(r"\b[0-9]{6}\b", result["reply"]["text"])[0]
    response = verify(api, row, code)
    assert response.status_code == 200, response.text
    return row, code


def test_timeline_connect_disconnect_reconnect_and_retries(wa):
    api, settings, _, now, _, _ = wa
    assert history(api) == {"data": [], "next_cursor": None}
    row = challenge(api)
    assert history(api)["data"] == []
    result = event(api, settings, incoming(row, now[0]))
    code = re.search(r"\b[0-9]{6}\b", result["reply"]["text"])[0]
    wrong = f"{(int(code) + 1) % 1000000:06d}"
    assert verify(api, row, wrong).status_code == 400
    assert history(api)["data"] == []
    assert verify(api, row, code).status_code == 200
    assert verify(api, row, code).status_code == 409
    first = history(api)["data"]
    assert len(first) == 1 and first[0]["event"] == "linked"
    assert first[0]["masked_number"] == "•••• 1111"
    assert first[0]["source"] == "action"
    assert "628111111111" not in json.dumps(first) and code not in json.dumps(first)
    now[0] += timedelta(seconds=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: api.delete("/api/v1/me/whatsapp"), range(2)))
    assert all(response.status_code == 200 for response in responses)
    assert [row["event"] for row in history(api)["data"]] == ["disconnected", "linked"]
    now[0] += timedelta(seconds=61)
    connect(wa)
    rows = history(api)["data"]
    assert [row["event"] for row in rows] == ["linked", "disconnected", "linked"]
    assert len({row["id"] for row in rows}) == 3
    assert rows[-1] == first[0]
    now[0] += timedelta(days=2)
    assert history(api)["data"] == rows


def test_latest_link_read_skips_disconnect_and_linking_maintenance(wa):
    api, _, db, now, _, _ = wa
    connect(wa)
    now[0] += timedelta(seconds=61)
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    connect(wa)
    latest_at = now[0]
    now[0] += timedelta(days=2)
    assert api.delete("/api/v1/me/whatsapp").status_code == 200

    statements = []

    def record(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement.lower().lstrip())

    sql_event.listen(db.engine, "before_cursor_execute", record)
    try:
        response = history(api, limit=1, event="linked")
    finally:
        sql_event.remove(db.engine, "before_cursor_execute", record)

    assert len(response["data"]) == 1
    row = response["data"][0]
    assert row["event"] == "linked"
    assert datetime.fromisoformat(row["occurred_at"]) == latest_at
    assert response["next_cursor"]
    assert not any(
        "pg_advisory" in sql or "cashly.whatsapp_integration" in sql for sql in statements
    )
    assert not any(sql.startswith(("update ", "insert ", "delete ")) for sql in statements)
    assert sum(sql.startswith("select ") for sql in statements) == 5
    assert len(history(api)["data"]) == 4


def test_history_account_isolation_rls_and_append_only(wa):
    api, settings, db, _, identities, accounts = wa
    connect(wa)
    own = history(api)["data"]
    api.app.dependency_overrides[require_identity] = lambda: identities[1]
    assert history(api)["data"] == []
    connect(wa, sender="628111111112@s.whatsapp.net")
    other = history(api)["data"]
    assert len(other) == 1 and other[0]["id"] != own[0]["id"]
    with db.transaction() as session:
        assert (
            session.execute(text("SELECT count(*) FROM cashly.whatsapp_link_history")).scalar() == 0
        )
        for privilege in ("UPDATE", "DELETE"):
            assert not session.execute(
                text(
                    "SELECT has_table_privilege("
                    "current_user,'cashly.whatsapp_link_history',:privilege)"
                ),
                {"privilege": privilege},
            ).scalar()
    with db.transaction(account_id=accounts[0].account_id) as session:
        owners = (
            session.execute(text("SELECT account_id FROM cashly.whatsapp_link_history"))
            .scalars()
            .all()
        )
        assert owners == [accounts[0].account_id]
    with pytest.raises(DBAPIError):
        with db.transaction(account_id=accounts[0].account_id) as session:
            session.add(
                WhatsAppLinkHistory(
                    account_id=accounts[1].account_id,
                    connection_id=uuid4(),
                    event="linked",
                    occurred_at=wa[3][0],
                    phone_last4="1111",
                    source="action",
                )
            )
    admin = build_engine(settings, migration=True)
    with admin.begin() as connection:
        connection.execute(
            text("DELETE FROM auth.users WHERE id=:id"), {"id": identities[0].user_id}
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM cashly.whatsapp_link_history WHERE account_id=:id"),
                {"id": accounts[0].account_id},
            ).scalar()
            == 0
        )
        assert (
            connection.execute(text("SELECT count(*) FROM cashly.whatsapp_link_history")).scalar()
            == 1
        )
    admin.dispose()


def test_history_pagination_with_identical_timestamps_and_bad_cursors(wa):
    api, settings, _, now, _, accounts = wa
    admin = build_engine(settings, migration=True)
    with admin.begin() as connection:
        for _ in range(13):
            connection.execute(
                text(
                    "INSERT INTO cashly.whatsapp_link_history "
                    "(id,account_id,connection_id,event,occurred_at,phone_last4,source) "
                    "VALUES (:id,:account,:connection,'linked',:at,'1111','action')"
                ),
                {
                    "id": uuid4(),
                    "account": accounts[0].account_id,
                    "connection": uuid4(),
                    "at": now[0],
                },
            )
    admin.dispose()
    all_rows, cursor = [], None
    while True:
        page = history(api, limit=4, **({"cursor": cursor} if cursor else {}))
        all_rows.extend(page["data"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(all_rows) == len({row["id"] for row in all_rows}) == 13
    assert [row["id"] for row in all_rows] == sorted((row["id"] for row in all_rows), reverse=True)
    malformed = [
        "bad!",
        base64.urlsafe_b64encode(json.dumps([{}, {}]).encode()).decode(),
        base64.urlsafe_b64encode(
            json.dumps(["2026-11-21T00:00:00", str(uuid4())]).encode()
        ).decode(),
    ]
    for cursor in malformed:
        assert api.get("/api/v1/me/whatsapp/history", params={"cursor": cursor}).status_code == 422
    for limit in (0, 51):
        assert api.get("/api/v1/me/whatsapp/history", params={"limit": limit}).status_code == 422


def test_history_backfills_only_existing_active_connections(wa):
    api, settings, _, now, _, _ = wa
    connect(wa)
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    now[0] += timedelta(seconds=61)
    connect(wa)
    assert len(history(api)["data"]) == 3
    command.downgrade(alembic_config(settings), "0007_whatsapp_linking")
    prepare(settings, [])
    rows = history(api)["data"]
    assert len(rows) == 1 and rows[0]["source"] == "existing_connection"
    assert rows[0]["event"] == "linked"
    assert datetime.fromisoformat(rows[0]["occurred_at"]) == now[0]
    now[0] += timedelta(seconds=1)
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    assert [row["event"] for row in history(api)["data"]] == ["disconnected", "linked"]


def test_history_insert_failure_rolls_back_channel_changes(wa, monkeypatch):
    api, settings, db, now, identities, _ = wa
    row = challenge(api)
    reply = event(api, settings, incoming(row, now[0]))
    code = re.search(r"\b[0-9]{6}\b", reply["reply"]["text"])[0]

    def broken_history(**kwargs):
        return WhatsAppLinkHistory(**(kwargs | {"source": "invalid-source"}))

    with monkeypatch.context() as patch:
        patch.setattr(whatsapp, "LinkHistory", broken_history)
        assert verify(api, row, code).status_code == 500
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "not_linked"
    assert history(api)["data"] == []
    assert verify(api, row, code).status_code == 200
    with monkeypatch.context() as patch:
        patch.setattr(whatsapp, "LinkHistory", broken_history)
        assert api.delete("/api/v1/me/whatsapp").status_code == 500
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "connected"
    assert len(history(api)["data"]) == 1
