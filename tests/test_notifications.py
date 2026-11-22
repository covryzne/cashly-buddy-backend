from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text

from cashly.api import push_test
from cashly.core.auth import Identity, require_identity
from cashly.core.settings import Settings
from cashly.db.commands import upgrade_database
from cashly.main import create_app
from cashly.services import web_push
from cashly.services.notification_worker import claim_delivery, create_reminders, finish_delivery

ROOT = "/api/v1/notifications"


@pytest.mark.parametrize(
    "environment,staging", [("staging", True), ("dev", False), ("prod", False)]
)
def test_worker_uses_the_environment_schedule(monkeypatch, environment, staging):
    calls = []

    async def worker(_db, _key, _subject, *, staging, production_times):
        calls.append((staging, production_times))

    monkeypatch.setattr("cashly.main.run_notification_worker", worker)
    settings = Settings(
        environment=environment,
        database_url=SecretStr("postgresql://fixture@127.0.0.1:5432/postgres?sslmode=disable"),
        notification_worker_enabled=True,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/health").is_success
    assert calls == [(staging, (time(8), time(12), time(19)) if environment == "prod" else None)]


def test_web_push_matches_rfc8291_example():
    encrypted = web_push.encrypt(
        web_push.decode("V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"),
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender=web_push.signing_key("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        salt=web_push.decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert web_push.encode(encrypted) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
        "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
        "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_daily_reminder_inbox_subscription_and_account_scope(postgres_settings, monkeypatch):
    upgrade_database(postgres_settings)
    settings = postgres_settings.model_copy(
        update={
            "environment": "staging",
            "web_push_private_key": SecretStr(web_push.encode((27).to_bytes(32, "big"))),
            "web_push_subject": "mailto:fixture@example.invalid",
        }
    )
    deliveries = []

    async def accept_push(_client, row, _key, _subject, *, test=False):
        deliveries.append((row["endpoint"], test))
        return 201

    monkeypatch.setattr(push_test, "DELAY_SECONDS", 0)
    monkeypatch.setattr(web_push, "send", accept_push)
    app = create_app(settings)
    auth_a, auth_b, account_a, account_b = (uuid4() for _ in range(4))
    now = datetime(2026, 11, 22, 1, 42, tzinfo=UTC)
    preference_now = now - timedelta(minutes=1)

    class PreferenceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return preference_now.astimezone(tz)

    monkeypatch.setattr("cashly.api.notifications.datetime", PreferenceClock)
    selected_user = auth_a
    app.dependency_overrides[require_identity] = lambda: Identity(
        selected_user, "fixture@example.invalid", "Fixture", now
    )

    with TestClient(app) as client:
        database = app.state.database
        with database.engine.begin() as connection:
            assert (
                connection.scalar(
                    text("""
              SELECT label_id FROM cashly.expense_categories WHERE code = 'food_drink'
            """)
                )
                == "Makanan dan Minuman"
            )
            for account, auth in ((account_a, auth_a), (account_b, auth_b)):
                params = {"account": account, "auth": auth, "username": f"test_{account.hex[:10]}"}
                connection.execute(text("INSERT INTO auth.users(id) VALUES (:auth)"), params)
                connection.execute(
                    text("INSERT INTO cashly.accounts(id) VALUES (:account)"), params
                )
                connection.execute(
                    text("""
                  INSERT INTO cashly.profiles (account_id, display_name, username)
                  VALUES (:account, 'Fixture', :username)
                """),
                    params,
                )
                connection.execute(
                    text("""
                  INSERT INTO cashly.account_auth_links (auth_user_id, account_id, verified_at)
                  VALUES (:auth, :account, now())
                """),
                    params,
                )
            connection.execute(
                text("""
              INSERT INTO cashly.game_events
                (id, account_id, event_key, kind, local_date, timezone, xp, freeze_delta)
              VALUES (gen_random_uuid(), :account, 'fixture', 'checkin', :day, 'Asia/Jakarta', 0, 0)
            """),
                {"account": account_b, "day": now.date()},
            )
            connection.execute(
                text("""
                  INSERT INTO cashly.expenses
                    (id, account_id, amount_idr, occurred_at, category_code, description,
                     payment_method, source_platform, source_type, source_message_id,
                     source_item_index, timestamp_basis)
                  VALUES
                    (gen_random_uuid(), :account, 1000, :occurred, 'food_drink', 'Tes',
                     'cash', 'dashboard', 'dashboard', gen_random_uuid()::text, 0, 'user_explicit')
                """),
                {"account": account_b, "occurred": now - timedelta(minutes=2)},
            )
        defaults = client.get(f"{ROOT}/preferences").json()["data"]
        assert defaults["daily_enabled"] is False and defaults["push_public_key"]
        assert defaults["test_push_available"] is True
        reminder = {"daily_enabled": True, "reminder_time": "08:42"}
        assert client.patch(f"{ROOT}/preferences", json=reminder).is_success
        endpoint = "https://fcm.googleapis.com/fcm/send/fixture-endpoint"
        keys = {
            "endpoint": endpoint,
            "p256dh": web_push.encode(
                web_push.public_bytes(ec.generate_private_key(ec.SECP256R1()))
            ),
            "auth": web_push.encode(bytes(16)),
        }
        assert client.post(f"{ROOT}/push", json=keys).is_success
        device = client.post(f"{ROOT}/preferences/device", json={"endpoint": endpoint}).json()[
            "data"
        ]
        assert device["push_active"] is True
        assert device["daily_enabled"] is True and device["reminder_time"] == "08:42"
        assert (
            client.post(f"{ROOT}/push", json=dict(keys, endpoint="https://127.0.0.1/p")).status_code
            == 422
        )
        selected_user = auth_b
        assert client.patch(f"{ROOT}/preferences", json=reminder).is_success
        assert client.post(f"{ROOT}/push", json=keys).status_code == 409
        assert client.post(f"{ROOT}/push/status", json={"endpoint": endpoint}).json() == {
            "data": {"active": False}
        }
        assert client.post(f"{ROOT}/push/test", json={"endpoint": endpoint}).status_code == 404
        second_endpoint = f"{endpoint}-b"
        assert client.post(f"{ROOT}/push", json=dict(keys, endpoint=second_endpoint)).is_success
        second_device = {"endpoint": second_endpoint}
        assert client.post(f"{ROOT}/push/test", json=second_device).status_code == 202
        assert client.post(f"{ROOT}/push/test", json=second_device).status_code == 429
        assert deliveries == [(second_endpoint, True)]
        assert create_reminders(database, now, True) == (1, 1)
        assert create_reminders(database, now, True) == (0, 0)
        assert client.get(ROOT).json()["data"]["items"] == []
        selected_user = auth_a
        inbox = client.get(ROOT).json()["data"]
        assert inbox["unread_count"] == 1
        assert len(inbox["items"]) == 1
        entry_id = inbox["items"][0]["id"]
        with database.engine.begin() as connection:
            connection.execute(
                text("""
              UPDATE cashly.push_deliveries SET available_at = now()
              WHERE notification_id = :id
            """),
                {"id": entry_id},
            )
        delivery = claim_delivery(database)
        assert str(delivery["notification_id"]) == entry_id
        assert claim_delivery(database) is None
        finish_delivery(database, delivery, 410)
        assert client.post(f"{ROOT}/push/status", json={"endpoint": endpoint}).json() == {
            "data": {"active": False}
        }
        selected_user = auth_b
        assert client.post(f"{ROOT}/{entry_id}/read").status_code == 404
        selected_user = auth_a
        assert client.post(f"{ROOT}/{entry_id}/read").is_success
        first_read = client.get(ROOT).json()["data"]["items"][0]["read_at"]
        assert first_read is not None
        assert client.post(f"{ROOT}/read-all").is_success
        inbox = client.get(ROOT).json()["data"]
        assert inbox["unread_count"] == 0
        assert inbox["items"][0]["read_at"] == first_read

        # The staging scheduler still waits for 08:42 and creates one reminder after check-in.
        selected_user = auth_b
        assert create_reminders(database, now - timedelta(minutes=1), True, staging=True) == (0, 0)
        assert create_reminders(database, now, True, staging=True) == (1, 1)
        assert create_reminders(database, now, True, staging=True) == (0, 0)
        assert len(client.get(ROOT).json()["data"]["items"]) == 1

        preference_now = now + timedelta(minutes=1)
        later = {"daily_enabled": True, "reminder_time": "08:45"}
        assert client.patch(f"{ROOT}/preferences", json=later).is_success
        assert create_reminders(database, preference_now, True, staging=True) == (0, 0)
        assert claim_delivery(database) is None
        later_due = now + timedelta(minutes=3)
        assert create_reminders(database, later_due, True) == (0, 0)
        assert create_reminders(database, later_due, True, staging=True) == (1, 1)
        assert create_reminders(database, later_due, True, staging=True) == (0, 0)
        assert len(client.get(ROOT).json()["data"]["items"]) == 2

        preference_now = later_due + timedelta(minutes=1)
        past = {"daily_enabled": True, "reminder_time": "08:44"}
        assert client.patch(f"{ROOT}/preferences", json=past).is_success
        assert create_reminders(database, preference_now, True, staging=True) == (0, 0)

        selected_user = auth_a
        assert client.patch(f"{ROOT}/preferences", json=later).is_success
        assert create_reminders(database, preference_now, True) == (0, 0)
        assert len(client.get(ROOT).json()["data"]["items"]) == 1

        selected_user = auth_b
        assert client.delete(ROOT).json() == {"data": {"cleared": 2}}
        assert client.delete(ROOT).json() == {"data": {"cleared": 0}}
        assert client.get(ROOT).json()["data"] == {
            "items": [],
            "next_cursor": None,
            "unread_count": 0,
        }
        selected_user = auth_a
        assert len(client.get(ROOT).json()["data"]["items"]) == 1

    production_settings = settings.model_copy(update={"environment": "prod"})
    production = create_app(production_settings)
    production.dependency_overrides[require_identity] = app.dependency_overrides[require_identity]
    production_times = production_settings.production_reminder_times
    tomorrow = now.replace(hour=1, minute=0) + timedelta(days=1)
    with TestClient(production) as client:
        database = production.state.database
        assert client.patch(f"{ROOT}/preferences", json={"daily_enabled": False}).is_success
        selected_user = auth_b
        # A legacy client still cannot replace the fixed production hours.
        response = client.patch(
            f"{ROOT}/preferences", json={"daily_enabled": True, "reminder_time": "08:42"}
        )
        assert response.is_success
        selected = response.json()["data"]
        assert selected["reminder_time"] == "08:00"
        assert selected["production_reminder_times"] == ["08:00", "12:00", "19:00"]
        assert selected["test_push_available"] is False
        assert client.patch(f"{ROOT}/preferences", json={"daily_enabled": True}).is_success
        assert client.post(f"{ROOT}/push/test", json=second_device).status_code == 404

        with database.engine.begin() as connection:
            connection.execute(
                text("""
                  INSERT INTO cashly.game_events
                    (id, account_id, event_key, kind, local_date, timezone, xp, freeze_delta)
                  VALUES
                    (gen_random_uuid(), :account, 'prod-fixture', 'checkin',
                     :day, 'Asia/Jakarta', 0, 0)
                """),
                {"account": account_b, "day": tomorrow.date()},
            )
        for hours_after_morning in (0, 4, 11):
            slot = tomorrow + timedelta(hours=hours_after_morning)
            assert create_reminders(database, slot, True, production_times=production_times) == (
                1,
                1,
            )
            assert create_reminders(database, slot, True, production_times=production_times) == (
                0,
                0,
            )
        before_clear = client.get(ROOT).json()["data"]
        assert len(before_clear["items"]) == 3
        cursor = before_clear["items"][0]["id"]
        assert client.delete(ROOT).json() == {"data": {"cleared": 3}}
        assert client.get(ROOT, params={"before": cursor}).json()["data"]["items"] == []
        assert client.get(ROOT).json()["data"]["unread_count"] == 0
        assert create_reminders(database, slot, True, production_times=production_times) == (0, 0)
        with database.engine.begin() as connection:
            connection.execute(
                text("""
              UPDATE cashly.push_deliveries SET available_at = now() WHERE notification_id = :id
            """),
                {"id": cursor},
            )
        assert claim_delivery(database) is None
        with database.engine.begin() as connection:
            slots = connection.scalars(
                text("""
                  SELECT production_reminder_time FROM cashly.notifications
                  WHERE account_id = :account AND local_day = :day
                  ORDER BY production_reminder_time
                """),
                {"account": account_b, "day": tomorrow.date()},
            ).all()
        assert tuple(slots) == production_times
