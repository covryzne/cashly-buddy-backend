"""Provider contract simulation: real HTTP to loopback, no key/env or paid calls."""

import asyncio
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from cashly.core.auth import Identity, require_identity
from cashly.core.errors import ApiError
from cashly.core.logging import JsonFormatter, TerminalFormatter
from cashly.core.settings import Settings
from cashly.main import create_app
from cashly.schemas.chat import ChatDraft, ChatExtractInput, ModelExtraction
from cashly.services.chat_extraction import apply_extraction, extract_chat, safe_provider_error


def test_provider_error_diagnostics_are_finite_labels_without_raw_private_data():
    response = httpx2.Response(
        503,
        json={
            "error": {
                "status": "UNAVAILABLE",
                "message": "Model overloaded private-secret",
                "details": [{"key": "private-key"}],
            }
        },
    )
    assert safe_provider_error(response) == {
        "provider_error_status": "UNAVAILABLE",
        "provider_error_reason": "capacity",
    }
    assert safe_provider_error(httpx2.Response(503, text="private-html")) == {}
    assert (
        safe_provider_error(
            httpx2.Response(
                503,
                json={
                    "error": {
                        "status": "private-status",
                        "message": "private-message",
                    }
                },
            )
        )
        == {}
    )


def test_interactions_error_code_is_mapped_to_a_finite_label():
    """The Interactions API returns a lowercase `code`, not an uppercase `status`."""
    response = httpx2.Response(
        503,
        json={
            "error": {
                "code": "service_unavailable",
                "message": "gemini-3.5-flash-lite is experiencing high demand, spikes are normal",
            }
        },
    )
    assert safe_provider_error(response) == {
        "provider_error_status": "UNAVAILABLE",
        "provider_error_reason": "capacity",
    }
    rate_limited = httpx2.Response(
        429,
        json={
            "error": {
                "code": "too_many_requests",
                "message": "Rate limit exceeded for model (limit: 5 requests per minute)",
            }
        },
    )
    assert safe_provider_error(rate_limited) == {
        "provider_error_status": "RESOURCE_EXHAUSTED",
        "provider_error_reason": "rate_limit",
    }
    assert (
        safe_provider_error(
            httpx2.Response(503, json={"error": {"code": "private-code", "message": "x"}})
        )
        == {}
    )


NOW = datetime(2026, 11, 18, 15, 30, tzinfo=ZoneInfo("Asia/Jakarta"))
CATEGORIES = {"food_drink": "Makanan", "transport": "Transportasi", "other": "Lainnya"}


def draft(**overrides):
    return {
        "merchant": "",
        "categoryCode": "food_drink",
        "paymentMethod": "",
        "occurredDate": "2026-11-17",
        "occurredTime": "12:00",
        "discountIdr": "0",
        "taxIdr": "0",
        "items": [{"id": "1", "name": "Kopi", "quantity": "1", "priceIdr": "18000"}],
        **overrides,
    }


def extraction(target=None, value=None, **overrides):
    return {
        "intent": "record",
        "question": None,
        "changes": [
            {
                "target_id": target,
                "draft": value or draft(),
                "question": None,
                "reason": None,
            }
        ],
        **overrides,
    }


def apply(body, result):
    return apply_extraction(
        ChatExtractInput.model_validate(body),
        ModelExtraction.model_validate(result),
        CATEGORIES,
        NOW,
    )


def test_complete_purchase_is_only_a_preview_draft():
    result = apply({"message": "kopi 18rb"}, extraction())
    assert len(result.entries) == 1
    assert result.entries[0].draft.items[0].priceIdr == "18000"
    assert "ditinjau" in result.reply
    assert "tersimpan" not in result.reply


def test_missing_merchant_uses_purchase_title_without_inventing_a_shop():
    result = apply({"message": "kopi 18rb"}, extraction(value=draft(merchant="")))
    assert result.entries[0].draft.merchant == result.entries[0].draft.items[0].name
    explicit = apply({"message": "kopi di Toko A 18rb"}, extraction(value=draft(merchant="Toko A")))
    assert explicit.entries[0].draft.merchant == "Toko A"


def test_clarification_correction_and_new_purchase_keep_entry_identity():
    pending = apply(
        {"message": "beli kopi"},
        {
            "intent": "clarify",
            "question": "Kopinya berapa?",
            "changes": [
                {
                    "target_id": None,
                    "draft": None,
                    "question": "Kopinya berapa?",
                    "reason": "amount",
                }
            ],
        },
    )
    entry = pending.entries[0]
    completed = apply(
        {"message": "18rb", "entries": [entry.model_dump(mode="json")]}, extraction(str(entry.id))
    )
    assert completed.entries[0].id == entry.id
    assert completed.entries[0].text == "beli kopi\n18rb"
    assert completed.entries[0].createdAt == entry.createdAt
    corrected = apply(
        {"message": "kopi tadi 20rb", "entries": completed.model_dump(mode="json")["entries"]},
        extraction(
            str(entry.id),
            draft(
                items=[
                    {
                        "id": "1",
                        "name": "Kopi",
                        "quantity": "1",
                        "priceIdr": "20000",
                    }
                ]
            ),
        ),
    )
    new = apply(
        {"message": "bensin 30rb", "entries": corrected.model_dump(mode="json")["entries"]},
        extraction(
            value=draft(
                categoryCode="transport",
                items=[
                    {
                        "id": "1",
                        "name": "Bensin",
                        "quantity": "1",
                        "priceIdr": "30000",
                    }
                ],
            )
        ),
    )
    assert len(new.entries) == 2
    assert new.entries[0] == corrected.entries[0]
    assert "Rp50.000" in new.reply


def test_out_of_scope_and_ambiguous_target_cannot_change_drafts():
    existing = apply({"message": "kopi 18rb"}, extraction()).model_dump(mode="json")["entries"]
    for intent, question in [("out_of_scope", None), ("clarify", "Kopi yang mana?")]:
        result = apply(
            {"message": "buat kode python", "entries": existing},
            {
                "intent": intent,
                "question": question,
                "changes": [],
            },
        )
        assert result.model_dump(mode="json")["entries"] == existing
    with pytest.raises(ApiError):
        apply({"message": "buat kode python"}, extraction(intent="out_of_scope"))


def test_manual_chat_uses_model_redirect_and_keeps_existing_drafts():
    existing = apply({"message": "kopi 18rb"}, extraction()).model_dump(mode="json")["entries"]
    for message, redirect in (
        ("modelnya apa?", "Detail modelnya nggak aku bahas di sini. Coba kirim kopi 18rb."),
        ("balasannya kok kaku?", "Mau lanjut mengecek draft kopi tadi bareng Mochi?"),
    ):
        result = apply(
            {"message": message, "entries": existing},
            {"intent": "out_of_scope", "question": redirect, "changes": []},
        )
        assert result.reply == redirect
        assert result.model_dump(mode="json")["entries"] == existing


@pytest.mark.parametrize(
    "redirect",
    [
        None,
        "   ",
        "Baris pertama\nAda pengeluaran?",
        "Buka https://example.invalid?",
        "x" * 180 + "?",
    ],
)
def test_manual_chat_falls_back_when_redirect_is_unusable(redirect):
    body = {"message": "modelnya apa?"}
    fallback = apply(body, {"intent": "out_of_scope", "question": None, "changes": []})
    result = apply(body, {"intent": "out_of_scope", "question": redirect, "changes": []})
    assert result == fallback


def test_unanswered_correction_keeps_the_complete_draft():
    existing = apply({"message": "kopi 18rb"}, extraction()).entries[0]
    result = apply(
        {"message": "ubah harga kopi", "entries": [existing.model_dump(mode="json")]},
        {
            "intent": "clarify",
            "question": "Harga barunya berapa?",
            "changes": [
                {
                    "target_id": str(existing.id),
                    "draft": None,
                    "question": "Harga barunya berapa?",
                    "reason": "amount",
                }
            ],
        },
    )
    assert result.entries == [existing]
    assert result.reply == "Harga barunya berapa?"


def test_unknown_target_category_future_date_and_cross_entry_edit_are_rejected():
    for result in [
        extraction(str(uuid4())),
        extraction(value=draft(categoryCode="invented")),
        extraction(value=draft(occurredDate="2026-11-19")),
    ]:
        with pytest.raises(ApiError):
            apply({"message": "kopi 18rb"}, result)
    entry = apply({"message": "kopi 18rb"}, extraction()).entries[0]
    with pytest.raises(ApiError):
        apply(
            {
                "message": "ubah kopi",
                "entries": [entry.model_dump(mode="json")],
                "edit_entry_id": str(entry.id),
            },
            extraction(),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"discountIdr": "19000"},
        {"taxIdr": "01"},
        {"occurredDate": "2026-02-30"},
        {"items": [{"id": "1", "name": "Kopi", "quantity": "0", "priceIdr": "18000"}]},
    ],
)
def test_invalid_money_dates_quantities_are_not_accepted_as_drafts(overrides):
    with pytest.raises(ValidationError):
        ChatDraft.model_validate(draft(**overrides))


@contextmanager
def provider(status=200, content=None, delay=0):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(
                {
                    "path": self.path,
                    "key": self.headers.get("x-goog-api-key"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    content
                    if content is not None
                    else {
                        "status": "completed",
                        "model": "gemini-3.8-flash",
                        "steps": [
                            {"type": "thought", "signature": "not-output"},
                            {
                                "type": "model_output",
                                "content": [{"type": "text", "text": json.dumps(extraction())}],
                            },
                        ],
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("status", "content", "expected", "code"),
    [
        (200, None, 200, None),
        (429, {}, 429, "AI_RATE_LIMITED"),
        (503, {"private": "never expose this"}, 503, "AI_UNAVAILABLE"),
        (400, {}, 502, "AI_REQUEST_REJECTED"),
        (401, {}, 503, "AI_ACCESS_DENIED"),
        (403, {}, 503, "AI_ACCESS_DENIED"),
        (404, {}, 503, "AI_MODEL_NOT_FOUND"),
        (200, {"status": "completed", "steps": []}, 502, "AI_INVALID_RESULT"),
        (200, {"status": "incomplete", "steps": []}, 502, "AI_INVALID_RESULT"),
        (200, {"status": "requires_action", "steps": []}, 502, "AI_INVALID_RESULT"),
        (
            200,
            {"status": "completed", "steps": [{"type": "thought", "text": "not-answer"}]},
            502,
            "AI_INVALID_RESULT",
        ),
    ],
)
def test_authenticated_endpoint_uses_real_http_and_sanitizes_failures(
    monkeypatch,
    status,
    content,
    expected,
    code,
):
    monkeypatch.setattr("cashly.api.chat.database", lambda request: object())
    monkeypatch.setattr(
        "cashly.api.chat.extraction_context",
        lambda db, identity: (
            CATEGORIES,
            "Asia/Jakarta",
        ),
    )
    with provider(status, content) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        app = create_app(Settings(environment="dev", gemini_api_key=SecretStr("fake-test-key")))
        identity = Identity(uuid4(), "test@example.invalid", "Tester", datetime.now(UTC))
        app.dependency_overrides[require_identity] = lambda: identity
        with TestClient(app) as client:
            result = client.post("/api/v1/chat/extract", json={"message": "kopi 18rb"})
            assert result.status_code == expected, result.text
            assert result.headers["cache-control"] == "private, no-store"
            assert "fake-test-key" not in result.text
            assert "never expose this" not in result.text
            if code:
                assert result.json()["error"]["code"] == code
            assert len(calls) == 1
            assert calls[0]["path"] == "/interactions"
            assert calls[0]["key"] == "fake-test-key"
            request = calls[0]["body"]
            assert request["response_format"]["mime_type"] == "application/json"
            assert "properties" in request["response_format"]["schema"]
            assert request["generation_config"]["thinking_level"] == "low"
            assert request["store"] is False
            assert json.loads(request["input"])["message"] == "kopi 18rb"
            assert "system_instruction" in request
            assert "contents" not in request
            assert '"maxItems"' not in json.dumps(request["response_format"]["schema"])


def test_missing_key_and_unauthenticated_requests_do_not_call_provider(monkeypatch):
    app = create_app(Settings(environment="dev"))
    with TestClient(app) as client:
        assert client.post("/api/v1/chat/extract", json={"message": "kopi"}).status_code == 401
        app.dependency_overrides[require_identity] = lambda: Identity(
            uuid4(),
            "test@example.invalid",
            "Tester",
            datetime.now(UTC),
        )
        monkeypatch.setattr("cashly.api.chat.database", lambda request: object())
        monkeypatch.setattr(
            "cashly.api.chat.extraction_context",
            lambda db, identity: (
                CATEGORIES,
                "Asia/Jakarta",
            ),
        )
        result = client.post("/api/v1/chat/extract", json={"message": "kopi"})
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "AI_NOT_CONFIGURED"
        assert result.json()["error"]["message"] == (
            "Mochi belum bisa memproses pesanmu. Tunggu sebentar, lalu coba kirim lagi, ya."
        )


def test_model_metadata_visible_in_both_log_formats_without_content():
    record = logging.LogRecord(
        "cashly.ai", logging.INFO, "", 0, "ai_extraction_completed", (), None
    )
    record.provider, record.model, record.attempt = "gemini", "gemini-3.8-flash", 1
    record.outcome, record.duration_ms, record.request_id = "record", 125, "test-id"
    record.thinking_level, record.exception_type = "low", "ReadTimeout"
    record.api_key, record.message_text = "private-key", "private-chat"
    for formatter in [JsonFormatter(), TerminalFormatter(color=False)]:
        output = formatter.format(record)
        assert "gemini-3.8-flash" in output
        assert "ReadTimeout" in output and "low" in output
        assert "private-key" not in output and "private-chat" not in output


@pytest.mark.parametrize("model", ["gemini-3.8-flash", "gemini-2.5-flash"])
def test_auto_or_legacy_model_omits_model_specific_configuration(monkeypatch, model):
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)

        async def run():
            async with httpx2.AsyncClient(trust_env=False) as client:
                result = await extract_chat(
                    Settings(
                        environment="dev",
                        gemini_api_key=SecretStr("fake-key"),
                        gemini_model=model,
                        gemini_thinking_level="auto" if model.startswith("gemini-3") else "low",
                    ),
                    client,
                    ChatExtractInput(message="kopi 18rb"),
                    CATEGORIES,
                    "Asia/Jakarta",
                    "test-auto",
                )
                assert not client.is_closed
                return result

        assert asyncio.run(run()).entries
        assert "thinking_level" not in calls[0]["body"]["generation_config"]


def test_real_http_read_timeout_logs_phase_without_retry_or_private_data(monkeypatch):
    with provider(delay=0.2) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        settings = Settings(environment="dev", gemini_api_key=SecretStr("private-key"))

        async def run():
            async with httpx2.AsyncClient(trust_env=False) as client:
                # Keep the real HTTP transport but shorten the read deadline for this test only.
                original_request = client.post

                async def short_request(*args, **kwargs):
                    kwargs["timeout"] = httpx2.Timeout(0.05, connect=2)
                    return await original_request(*args, **kwargs)

                with patch.object(client, "post", short_request):
                    return await extract_chat(
                        settings,
                        client,
                        ChatExtractInput(message="kopi 18rb"),
                        CATEGORIES,
                        "Asia/Jakarta",
                        "test-timeout",
                    )

        with patch("cashly.services.chat_extraction.logger.info") as log:
            with pytest.raises(ApiError) as error:
                asyncio.run(run())
        assert error.value.code == "AI_TIMEOUT"
        assert len(calls) == 1
        metadata = next(
            call.kwargs["extra"]
            for call in reversed(log.call_args_list)
            if call.args[0] == "ai_extraction_completed"
        )
        assert metadata["exception_type"] == "ReadTimeout"
        assert metadata["outcome"] == "timeout"
        assert "private-key" not in repr(metadata)


@pytest.mark.parametrize("oversized", ["changes", "items"])
def test_full_backend_bounds_still_apply_when_provider_schema_is_simplified(monkeypatch, oversized):
    result = extraction()
    if oversized == "changes":
        result["changes"] *= 51
    else:
        result["changes"][0]["draft"]["items"] *= 51
    response = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": json.dumps(result)},
                ],
            }
        ],
    }
    with provider(content=response) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)

        async def run():
            async with httpx2.AsyncClient(trust_env=False) as client:
                return await extract_chat(
                    Settings(environment="dev", gemini_api_key=SecretStr("private-key")),
                    client,
                    ChatExtractInput(message="kopi 18rb"),
                    CATEGORIES,
                    "Asia/Jakarta",
                    "test-bounds",
                )

        with pytest.raises(ApiError) as error:
            asyncio.run(run())
        assert error.value.code == "AI_INVALID_RESULT"
        assert len(calls) == 1
