from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_auth import auth_postgres_settings as auth_postgres_settings
from test_chat_extraction import draft
from test_receipt_extraction import photo
from test_voice_extraction import encoded_audio
from test_whatsapp_history import connect
from test_whatsapp_linking import claim, event, headers, incoming
from test_whatsapp_linking import wa as wa

from cashly.db.models import Expense
from cashly.repositories.accounts import set_account_context
from cashly.schemas.extraction import ChatDraft, ChatEntry, ChatExtractData
from cashly.schemas.voice import TranscriptResult
from cashly.services import whatsapp, whatsapp_capture


def admit(wa, kind, message="kopi 18rb", *, link=True):
    api, settings, _, now, _, _ = wa
    settings = settings.model_copy(update={"whatsapp_recording_enabled": True})
    api.app.state.settings = settings
    if link:
        row, _ = connect(wa)
    else:
        row = None
    return event(api, settings, incoming(row, now[0], message=message) | {"kind": kind})


def process(wa, job, **kwargs):
    api, settings, *_ = wa
    job_id = job["job_id"]
    response = api.post(
        f"/api/v1/integrations/whatsapp/jobs/{job_id}/process",
        headers=headers(settings) | {"Idempotency-Key": f"wa-process:{job_id}"},
        json={"event_id": job["event_id"], "kind": job["kind"]} | kwargs,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def result(now, *, entry_id=None, complete=True):
    entry = ChatEntry(
        id=entry_id or uuid4(),
        text="kopi",
        createdAt=now,
        draft=ChatDraft.model_validate(
            draft(occurredDate=(now - timedelta(days=1)).date().isoformat())
        )
        if complete
        else None,
        issue=None if complete else "Berapa harganya?",
        reason=None if complete else "amount",
    )
    return ChatExtractData(
        entries=[entry],
        reply="Siap" if complete else "Berapa harganya?",
        intent="record" if complete else "clarify",
    )


def expenses(wa):
    api, *_ = wa
    response = api.get("/api/v1/expenses")
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.parametrize("kind", ["text", "receipt", "voice"])
def test_linked_capture_commits_once_and_claims_reply(wa, monkeypatch, kind):
    api, settings, db, now, identities, accounts = wa
    calls = []

    async def chat(_settings, _client, body, *_args, media=None, input_kind="chat"):
        if media is not None:
            calls.append("transcription")
            return TranscriptResult(status="clear", transcript="kopi 18rb", issue=None)
        assert body.message == "kopi 18rb"
        calls.append(input_kind)
        return result(now[0])

    async def receipt(_settings, _client, body, *_args):
        assert body.image.mime_type == "image/png"
        calls.append("receipt")
        return result(now[0])

    monkeypatch.setattr(whatsapp_capture, "extract_chat", chat)
    monkeypatch.setattr(whatsapp_capture, "extract_receipt", receipt)
    job = admit(wa, kind)
    assert job["action"] == "process" and job["verified_link"]
    body = {"media": photo()} if kind == "receipt" else {}
    if kind == "voice":
        body = {"media": encoded_audio(format="ogg")}
    saved = process(wa, job, **body)
    assert saved["status"] == "saved" and saved["committed"]
    assert process(wa, job, **body) == saved
    assert (
        calls
        == {"text": ["chat"], "receipt": ["receipt"], "voice": ["transcription", "voice"]}[kind]
    )
    rows = expenses(wa)
    assert len(rows) == 1
    assert rows[0]["source"] == kind
    assert rows[0]["amount_idr"] == "18000"
    with db.transaction(auth_user_id=identities[0].user_id) as session:
        set_account_context(session, accounts[0].account_id)
        assert session.scalar(select(Expense.source_platform)) == "whatsapp"
    assert claim(api, settings, saved["reply"]["id"])["reply"]["text"] == saved["reply"]["text"]
    assert process(wa, job, **body)["status"] == "ignored"


def test_pending_receipt_is_saved_only_after_text_clarification(wa, monkeypatch):
    _, _, _, now, *_ = wa

    async def receipt(*_args):
        return result(now[0], complete=False)

    async def chat(_settings, _client, body, *_args):
        assert len(body.entries) == 1 and body.entries[0].draft is None
        return result(now[0], entry_id=body.entries[0].id)

    monkeypatch.setattr(whatsapp_capture, "extract_chat", chat)
    monkeypatch.setattr(whatsapp_capture, "extract_receipt", receipt)
    review = process(wa, admit(wa, "receipt"), media=photo())
    assert review["status"] == "needs_review" and not review["committed"]
    assert expenses(wa) == []
    saved = process(wa, admit(wa, "text", "18 ribu", link=False))
    assert saved["committed"] and expenses(wa)[0]["source"] == "receipt"


def test_whatsapp_redirect_carries_short_history_without_saving(wa, monkeypatch):
    _, _, _, now, *_ = wa
    first = "Model AI kamu apa?"
    answer = "Detail modelnya nggak aku bahas di sini."
    histories = []

    async def chat(_settings, _client, body, *_args):
        histories.append([(message.role, message.text) for message in body.history])
        return ChatExtractData(entries=body.entries, reply=answer, intent="out_of_scope")

    monkeypatch.setattr(whatsapp_capture, "extract_chat", chat)
    inputs = [first, "Oh jadi nggak mau ngasih tahu?", "Oke, paham", "Halo lagi"]
    for index, message in enumerate(inputs):
        if index == 3:
            now[0] += timedelta(minutes=11)
        response = process(wa, admit(wa, "text", message, link=index == 0))
        assert response["status"] == "needs_review" and response["reply"]["text"] == answer
        now[0] += timedelta(seconds=1)
    assert histories == [
        [],
        [("user", first), ("assistant", answer)],
        [("user", first), ("assistant", answer), ("user", inputs[1]), ("assistant", answer)],
        [],
    ]
    assert expenses(wa) == []


def test_disconnect_during_extraction_prevents_commit(wa, monkeypatch):
    api, _, db, now, identities, _ = wa

    async def chat(settings, *_args):
        whatsapp.disconnect(db, settings, identities[0])
        return result(now[0])

    monkeypatch.setattr(whatsapp_capture, "extract_chat", chat)
    response = process(wa, admit(wa, "text"))
    assert response["status"] == "ignored" and expenses(wa) == []
    assert api.get("/api/v1/me/whatsapp").json()["data"]["status"] == "not_linked"


def test_failed_media_and_expired_job_do_not_save(wa):
    api, settings, _, now, *_ = wa
    unlinked = admit(wa, "voice", link=False)
    assert unlinked["action"] == "reply" and not unlinked["verified_link"]
    job = admit(wa, "receipt")
    assert process(wa, job, input_error="MEDIA_FETCH_FAILED")["status"] == "failed"
    pending = admit(wa, "voice", link=False)
    now[0] += timedelta(minutes=6)
    retry = claim(api, settings)
    assert retry["event_id"] == pending["event_id"]
    assert "kirim lagi" in retry["reply"]["text"]
    assert expenses(wa) == []
