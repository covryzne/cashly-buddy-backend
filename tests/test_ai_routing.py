"""Actual HTTP fallback sequences, with deterministic local providers and no paid calls."""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx2
import pytest
from pydantic import SecretStr
from test_chat_extraction import extraction
from test_receipt_extraction import photo
from test_voice_extraction import encoded_audio

from cashly.core.environment import ConfigurationError
from cashly.core.errors import ApiError
from cashly.core.settings import Settings
from cashly.schemas.extraction import ChatExtractInput
from cashly.schemas.receipt import ReceiptImage
from cashly.schemas.voice import VoiceAudio
from cashly.services.chat_extraction import extract_chat
from cashly.services.voice_audio import normalize_audio


@contextmanager
def upstream(monkeypatch, outcomes):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "body": body})
            status, variant, delay = outcomes.get(body["model"], (200, "valid", 0))
            if delay:
                time.sleep(delay)
            content = body.get("messages", [{}, {}])[1].get("content")
            audio = isinstance(content, list) and content[-1].get("type") == "input_audio"
            value = (
                {"status": "clear", "transcript": "beli kopi 18rb", "issue": None}
                if audio
                else extraction()
            )
            if variant == "invalid":
                value = {"unexpected": "invalid-model-output"}
            if variant == "clarify":
                value = {
                    "intent": "clarify",
                    "question": "Berapa nominalnya?",
                    "changes": [
                        {
                            "target_id": None,
                            "draft": None,
                            "question": "Berapa nominalnya?",
                            "reason": "amount",
                        }
                    ],
                }
            if status != 200:
                envelope = {
                    "error": {"message": "upstream-private-details", "status": "UNAVAILABLE"}
                }
            elif self.path.endswith("/interactions"):
                envelope = {
                    "status": "completed",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [{"type": "text", "text": json.dumps(value)}],
                        }
                    ],
                }
            else:
                envelope = {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": json.dumps(value)}}
                    ]
                }
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(envelope).encode())
            except (OSError, ConnectionError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", base + "/gemini")
    monkeypatch.setattr(
        "cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", base + "/openrouter"
    )
    try:
        yield calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def settings(**changes):
    return Settings(
        environment="dev",
        gemini_api_key=SecretStr("private-google-key"),
        openrouter_api_key=SecretStr("private-or-key"),
        gemini_model="primary",
        **changes,
    )


def run(config, media=None, kind=None):
    async def request():
        async with httpx2.AsyncClient(trust_env=False) as client:
            return await extract_chat(
                config,
                client,
                ChatExtractInput(message="beli kopi 18rb"),
                {"food_drink": "Makanan", "other": "Lainnya"},
                "Asia/Jakarta",
                "test-fallback",
                media,
                kind,
            )

    return asyncio.run(request())


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_gemini_provider_errors_fall_back_to_paid_openrouter(monkeypatch, status):
    with upstream(monkeypatch, {"primary": (status, "valid", 0)}) as calls:
        result = run(settings())
    assert result.entries[0].draft.items[0].priceIdr == "18000"
    assert [call["body"]["model"] for call in calls] == ["primary", "google/gemini-3.5-flash-lite"]
    first_context = json.loads(calls[0]["body"]["input"])
    fallback_context = json.loads(calls[1]["body"]["messages"][1]["content"])
    assert first_context == fallback_context  # Relative dates share one captured clock.


def test_invalid_model_response_uses_next_model(monkeypatch):
    with upstream(monkeypatch, {"primary": (200, "invalid", 0)}) as calls:
        assert run(settings()).entries[0].draft is not None
    assert len(calls) == 2


def test_valid_clarification_is_terminal_and_does_not_get_overridden(monkeypatch):
    with upstream(monkeypatch, {"primary": (200, "clarify", 0)}) as calls:
        result = run(settings())
    assert result.entries[0].draft is None and result.reply == "Berapa nominalnya?"
    assert len(calls) == 1


def test_order_multiple_models_and_deduplication(monkeypatch):
    targets = [
        {"provider": "gemini", "model": "primary", "inputs": ["text"]},
        {"provider": "openrouter", "model": "first", "inputs": ["text"]},
        {"provider": "openrouter", "model": "first", "inputs": ["text"]},
        {"provider": "openrouter", "model": "second", "inputs": ["text"]},
    ]
    with upstream(monkeypatch, {"primary": (503, "valid", 0), "first": (429, "valid", 0)}) as calls:
        assert run(settings(ai_fallback_models=targets)).entries[0].draft
    assert [call["body"]["model"] for call in calls] == ["primary", "first", "second"]


@pytest.mark.parametrize("mode", ["chat", "receipt", "audio", "voice_text"])
def test_fallback_targets_are_filtered_by_actual_input_type(monkeypatch, mode):
    targets = [
        {"provider": "openrouter", "model": kind, "inputs": [kind]}
        for kind in ["text", "image", "audio"]
    ]
    media = (
        ReceiptImage(**photo())
        if mode == "receipt"
        else normalize_audio(VoiceAudio(**encoded_audio()))
        if mode == "audio"
        else None
    )
    with upstream(monkeypatch, {"primary": (503, "valid", 0)}) as calls:
        result = run(
            settings(ai_fallback_models=targets), media, "voice" if mode == "voice_text" else None
        )
    expected = "image" if mode == "receipt" else "audio" if mode == "audio" else "text"
    assert [call["body"]["model"] for call in calls] == ["primary", expected]
    assert result.status == "clear" if mode == "audio" else result.entries[0].draft is not None


def test_missing_key_skips_network_and_continues(monkeypatch):
    config = settings().model_copy(update={"gemini_api_key": None})
    with upstream(monkeypatch, {}) as calls:
        assert run(config).entries[0].draft
    assert len(calls) == 1 and calls[0]["path"] == "/openrouter/chat/completions"


def test_disabled_fallback_returns_original_error_without_extra_call(monkeypatch):
    with upstream(monkeypatch, {"primary": (503, "valid", 0)}) as calls:
        with pytest.raises(ApiError) as error:
            run(settings(ai_fallback_models=[]))
    assert error.value.code == "AI_UNAVAILABLE" and len(calls) == 1


def test_attempt_timeout_uses_next_model(monkeypatch):
    config = settings().model_copy(
        update={"ai_attempt_timeout_seconds": 0.05, "ai_total_timeout_seconds": 1}
    )
    with upstream(monkeypatch, {"primary": (200, "valid", 0.15)}) as calls:
        assert run(config).entries[0].draft
    assert len(calls) == 2


def test_global_deadline_prevents_an_unbounded_chain(monkeypatch):
    config = settings(
        ai_fallback_models=[
            {"provider": "openrouter", "model": kind, "inputs": ["text"]} for kind in ["one", "two"]
        ]
    ).model_copy(update={"ai_attempt_timeout_seconds": 0.05, "ai_total_timeout_seconds": 0.08})
    with upstream(
        monkeypatch, {"primary": (200, "valid", 0.2), "one": (200, "valid", 0.2)}
    ) as calls:
        with pytest.raises(ApiError) as error:
            run(config)
    assert error.value.code == "AI_TIMEOUT"
    # Scheduling/connection setup may consume the remaining budget before model one.
    assert [call["body"]["model"] for call in calls] in (["primary"], ["primary", "one"])


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '[{"provider":"unknown","inputs":["text"]}]',
        '[{"provider":"gemini","model":"invalid/path","inputs":["text"]}]',
        '[{"provider":"openrouter","inputs":[]}]',
    ],
)
def test_bad_env_chain_is_rejected_without_echoing_values(raw):
    with pytest.raises(ConfigurationError) as error:
        Settings.from_values({"CASHLY_ENV": "dev", "AI_FALLBACK_MODELS": raw})
    assert raw not in str(error.value)


def test_attempt_logs_identify_provider_model_and_reason_without_secrets(monkeypatch):
    with (
        upstream(monkeypatch, {"primary": (503, "valid", 0)}),
        patch("cashly.services.chat_extraction.logger.info") as log,
    ):
        run(settings())
    starts = [
        call.kwargs["extra"]
        for call in log.call_args_list
        if call.args[0] == "ai_extraction_started"
    ]
    assert [(row["provider"], row["model"], row["attempt"]) for row in starts] == [
        ("gemini", "primary", 1),
        ("openrouter", "google/gemini-3.5-flash-lite", 2),
    ]
    fallback = next(
        call.kwargs["extra"]
        for call in log.call_args_list
        if call.args[0] == "ai_provider_fallback"
    )
    assert fallback["from_provider"] == "gemini" and fallback["fallback_reason"] == "AI_UNAVAILABLE"
    assert "private-google-key" not in repr(log.call_args_list)
    assert "private-or-key" not in repr(log.call_args_list)
    assert "beli kopi" not in repr(log.call_args_list)


def test_cancellation_does_not_call_another_provider(monkeypatch):
    with upstream(monkeypatch, {"primary": (200, "valid", 0.2)}) as calls:

        async def cancelled():
            async with httpx2.AsyncClient(trust_env=False) as client:
                task = asyncio.create_task(
                    extract_chat(
                        settings(),
                        client,
                        ChatExtractInput(message="kopi 18rb"),
                        {"food_drink": "Makanan"},
                        "Asia/Jakarta",
                        "cancelled-test",
                    )
                )
                async with asyncio.timeout(2):
                    while not calls:
                        await asyncio.sleep(0.005)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        asyncio.run(cancelled())
    assert len(calls) == 1


def test_business_validation_failure_does_not_trigger_fallback(monkeypatch):
    def reject(*_args, **_kwargs):
        raise ApiError(422, "CHAT_CONTEXT_LIMIT", "Catatan terlalu panjang.")

    monkeypatch.setattr("cashly.services.chat_extraction.apply_extraction", reject)
    with upstream(monkeypatch, {}) as calls:
        with pytest.raises(ApiError) as error:
            run(settings())
    assert error.value.code == "CHAT_CONTEXT_LIMIT" and len(calls) == 1
