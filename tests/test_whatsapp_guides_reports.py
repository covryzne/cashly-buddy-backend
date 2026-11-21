from datetime import timedelta
from uuid import uuid4

from test_auth import auth_postgres_settings as auth_postgres_settings
from test_whatsapp_history import connect
from test_whatsapp_linking import claim, event, headers, incoming, verify
from test_whatsapp_linking import wa as wa

from cashly.db.models import Expense


def test_guide_welcomes_first_verified_link_and_remains_available(wa):
    api, settings, _, now, *_ = wa
    api.app.state.settings = settings.model_copy(
        update={
            "whatsapp_recording_enabled": True,
            "whatsapp_app_url": "https://cashly.example.invalid/",
        }
    )
    unlinked = event(api, settings, incoming(None, now[0], message="/help"))
    assert "verifikasi OTP" in unlinked["reply"]["text"]
    claim(api, settings, unlinked["reply"]["id"])
    row, otp = connect(wa)
    welcome = claim(api, settings)
    assert welcome["verified_link"] and welcome["message_id"].startswith("welcome:")
    assert all(
        label in welcome["reply"]["text"] for label in ("naspad", "VN", "/help", "https://cashly")
    )
    assert verify(api, row, otp).status_code == 409
    assert claim(api, settings) is None
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    now[0] += timedelta(minutes=1)
    connect(wa)
    assert claim(api, settings) is None
    linked = event(api, settings, incoming(None, now[0], message="/help"))
    assert "/laporan hari ini" in linked["reply"]["text"]


def test_reports_are_account_scoped_and_pdf_requires_current_claim(wa, monkeypatch):
    api, settings, db, now, _, accounts = wa
    api.app.state.settings = settings.model_copy(
        update={
            "whatsapp_report_frontend_url": "http://frontend:3000",
        }
    )
    unlinked = event(api, settings, incoming(None, now[0], message="/laporan hari ini"))
    assert "belum terhubung" in unlinked["reply"]["text"]
    connect(wa)
    claim(api, settings)
    for account, amount in zip(accounts, (11000, 49000), strict=True):
        with db.transaction(account_id=account.account_id) as session:
            session.add(
                Expense(
                    account_id=account.account_id,
                    occurred_at=now[0],
                    category_code="food_drink",
                    description="Kopi",
                    amount_idr=amount,
                    source_platform="dashboard",
                    source_type="text",
                    source_message_id=uuid4().hex,
                    timestamp_basis="user_explicit",
                )
            )
    summary = event(api, settings, incoming(None, now[0], message="/laporan hari ini"))
    assert "Rp11.000" in summary["reply"]["text"] and "Rp49.000" not in summary["reply"]["text"]
    guide = event(api, settings, incoming(None, now[0], message="/reportpdf"))
    assert "sampai 30 nov 2025" in guide["reply"]["text"]
    report = event(api, settings, incoming(None, now[0], message="/reportpdf bulan ini"))
    reply_id = report["reply"]["id"]
    assert report["reply"]["report_pdf"] is True
    root = f"/api/v1/integrations/whatsapp/outbox/{reply_id}"
    assert (
        api.get(f"{root}/report-data", headers={"X-Cashly-Report-Lease": str(uuid4())}).status_code
        == 404
    )
    lease = claim(api, settings, reply_id)["reply"]["lease_id"]
    lease_headers = {"X-Cashly-Report-Lease": lease}
    data = api.get(f"{root}/report-data", headers=lease_headers).json()["data"]
    assert [item["amount_idr"] for item in data["expenses"]] == ["11000"]
    assert api.get(f"{root}/report-pdf", headers=lease_headers).status_code == 401

    def render(url):
        assert url.startswith("http://frontend:3000/internal/whatsapp-report?") and lease in url
        return "JVBERi0="

    monkeypatch.setattr("cashly.api.whatsapp.render_report", render)
    document = api.get(f"{root}/report-pdf", headers={**lease_headers, **headers(settings)})
    assert document.status_code == 200 and document.json()["data"]["media"] == "JVBERi0="
    assert api.delete("/api/v1/me/whatsapp").status_code == 200
    assert api.get(f"{root}/report-data", headers=lease_headers).status_code == 404
