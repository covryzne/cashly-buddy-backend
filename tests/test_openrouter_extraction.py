"""Actual HTTP to a loopback OpenRouter stand-in; no developer keys or hosted writes."""

import asyncio
import importlib.util
import json
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from cashly.core.auth import Identity, require_identity
from cashly.core.errors import ApiError
from cashly.core.logging import JsonFormatter, TerminalFormatter
from cashly.core.settings import Settings
from cashly.main import create_app
from cashly.schemas.chat import ChatExtractInput
from cashly.services.chat_extraction import extract_chat


def valid_response():
    data = {
        "intent": "record",
        "question": None,
        "changes": [
            {
                "target_id": None,
                "question": None,
                "reason": None,
                "draft": {
                    "merchant": "",
                    "categoryCode": "food_drink",
                    "paymentMethod": "",
                    "occurredDate": "2026-11-17",
                    "occurredTime": "12:00",
                    "discountIdr": "0",
                    "taxIdr": "0",
                    "items": [
                        {
                            "id": "1",
                            "name": "Kopi",
                            "quantity": "1",
                            "priceIdr": "18000",
                        }
                    ],
                },
            }
        ],
    }
    return {
        "model": "synthetic/model:free",
        "provider": "Synthetic Provider",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(data)},
            }
        ],
    }


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
                    "authorization": self.headers.get("Authorization"),
                    "google_key": self.headers.get("x-goog-api-key"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(content if content is not None else valid_response()).encode()
                )
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

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
        (400, {}, 502, "AI_REQUEST_REJECTED"),
        (401, {}, 503, "AI_ACCESS_DENIED"),
        (403, {}, 503, "AI_ACCESS_DENIED"),
        (402, {}, 503, "AI_CREDITS_REQUIRED"),
        (404, {}, 503, "AI_MODEL_NOT_FOUND"),
        (429, {}, 429, "AI_RATE_LIMITED"),
        (503, {"private": "upstream-secret"}, 503, "AI_UNAVAILABLE"),
        (200, {"error": {"code": 429, "message": "upstream-secret"}}, 429, "AI_RATE_LIMITED"),
        (200, {"error": {"code": 402, "message": "upstream-secret"}}, 503, "AI_CREDITS_REQUIRED"),
        (200, {"choices": []}, 502, "AI_INVALID_RESULT"),
        (
            200,
            {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
            502,
            "AI_INVALID_RESULT",
        ),
        (
            200,
            {"choices": [{"finish_reason": "stop", "message": {"content": "broken JSON"}}]},
            502,
            "AI_INVALID_RESULT",
        ),
        (
            200,
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "{}", "refusal": "blocked"}}
                ]
            },
            502,
            "AI_INVALID_RESULT",
        ),
    ],
)
def test_openrouter_endpoint_contract_and_safe_failures(
    monkeypatch, status, content, expected, code
):
    monkeypatch.setattr("cashly.api.chat.database", lambda request: object())
    monkeypatch.setattr(
        "cashly.api.chat.extraction_context",
        lambda db, identity: (
            {"food_drink": "Makanan", "other": "Lainnya"},
            "Asia/Jakarta",
        ),
    )
    with provider(status, content) as (url, calls):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        settings = Settings(
            environment="dev",
            ai_provider="openrouter",
            openrouter_api_key=SecretStr("private-or-key"),
            gemini_api_key=SecretStr("private-google-key"),
        )
        app = create_app(settings)
        identity = Identity(uuid4(), "test@example.invalid", "Tester", datetime.now(UTC))
        app.dependency_overrides[require_identity] = lambda: identity
        with patch("cashly.services.chat_extraction.extract_gemini_chat") as google_client:
            with TestClient(app) as client:
                result = client.post("/api/v1/chat/extract", json={"message": "kopi 18rb"})
            google_client.assert_not_called()
        assert result.status_code == expected, result.text
        assert result.headers["cache-control"] == "private, no-store"
        assert all(
            secret not in result.text
            for secret in ["private-or-key", "private-google-key", "upstream-secret"]
        )
        if code:
            assert result.json()["error"]["code"] == code
        else:
            assert result.json()["data"]["entries"][0]["draft"]["items"][0]["priceIdr"] == "18000"
        assert len(calls) == 1
        assert calls[0]["path"] == "/chat/completions"
        assert calls[0]["authorization"] == "Bearer private-or-key"
        assert calls[0]["google_key"] is None
        body = calls[0]["body"]
        assert body["model"] == "google/gemini-3.5-flash-lite"
        assert body["provider"] == {"require_parameters": True}
        # A cap above the affordable budget makes OpenRouter answer 402 for priced models.
        assert body["max_tokens"] == 4096
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        assert body["stream"] is False
        assert (
            "models" not in body
        )  # Model fallback is routed/logged by the application, not this API parameter.


@pytest.mark.parametrize("openrouter_key", [None, "", "   ", SecretStr("")])
def test_missing_openrouter_key_falls_back_to_gemini(monkeypatch, openrouter_key):
    settings = Settings(
        environment="dev",
        ai_provider="openrouter",
        gemini_api_key=SecretStr("private-google-key"),
        openrouter_api_key=openrouter_key,
    )
    content = {
        "status": "completed",
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": valid_response()["choices"][0]["message"]["content"]},
                ],
            }
        ],
    }
    with provider(content=content) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)

        async def run():
            async with httpx2.AsyncClient(trust_env=False) as client:
                return await extract_chat(
                    settings,
                    client,
                    ChatExtractInput(message="kopi 18rb"),
                    {"food_drink": "Makanan"},
                    "Asia/Jakarta",
                    "test-missing",
                )

        with patch("cashly.services.chat_extraction.logger.info") as log:
            result = asyncio.run(run())
        assert result.entries[0].draft.items[0].priceIdr == "18000"
        assert len(calls) == 1
        assert calls[0]["path"] == "/interactions"
        assert calls[0]["google_key"] == "private-google-key"
        assert calls[0]["authorization"] is None
        fallback = next(
            call.kwargs["extra"]
            for call in log.call_args_list
            if call.args[0] == "ai_provider_fallback"
        )
        assert fallback["provider"] == "gemini"
        assert fallback["requested_provider"] == "openrouter"
        assert fallback["fallback_reason"] == "openrouter_key_missing"
        import logging

        record = logging.LogRecord(
            "cashly.ai", logging.INFO, "", 0, "ai_provider_fallback", (), None
        )
        for key, value in fallback.items():
            setattr(record, key, value)
        for formatter in [JsonFormatter(), TerminalFormatter(color=False)]:
            output = formatter.format(record)
            assert "openrouter_key_missing" in output
            assert "private-google-key" not in output


def test_no_provider_key_returns_configuration_error_without_network():
    settings = Settings(environment="dev", ai_provider="openrouter")

    async def run():
        async with httpx2.AsyncClient() as client:
            return await extract_chat(
                settings,
                client,
                ChatExtractInput(message="kopi 18rb"),
                {"food_drink": "Makanan"},
                "Asia/Jakarta",
                "test-missing",
            )

    with patch("httpx2.AsyncClient.post") as google_client:
        with pytest.raises(ApiError) as error:
            asyncio.run(run())
        assert error.value.code == "AI_NOT_CONFIGURED"
        google_client.assert_not_called()


@pytest.mark.parametrize(
    ("provider_name", "or_key", "expected"),
    [
        ("gemini", None, "gemini"),
        ("gemini", "key", "gemini"),
        ("openrouter", None, "gemini"),
        ("openrouter", "key", "openrouter"),
        ("", None, "gemini"),
        (" OPENROUTER ", "key", "openrouter"),
    ],
)
def test_provider_env_selection(provider_name, or_key, expected):
    settings = Settings.from_values(
        {
            "CASHLY_ENV": "dev",
            "AI_PROVIDER": provider_name,
            "OPENROUTER_API_KEY": or_key or "",
        }
    )
    assert settings.effective_ai_provider == expected


def test_diagnostic_reports_gemini_when_selected_openrouter_has_no_key(capsys):
    spec = importlib.util.spec_from_file_location(
        "ai_fallback_diagnostic",
        Path(__file__).resolve().parents[1] / "scripts/check_ai.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = Settings(
        environment="dev",
        ai_provider="openrouter",
        gemini_model="gemini-3.5-flash",
        gemini_api_key=SecretStr("private-google-key"),
    )
    assert asyncio.run(module.check(settings)) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["requested_provider"] == "openrouter"
    assert result["provider"] == "gemini"
    assert result["model"] == "gemini-3.5-flash"
    assert result["fallback_reason"] == "openrouter_key_missing"
    assert result["key_configured"] is True
    assert "private-google-key" not in output


def test_generic_diagnostic_and_logs_report_selected_and_resolved_model(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "ai_diagnostic",
        Path(__file__).resolve().parents[1] / "scripts/check_ai.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        settings = Settings(
            environment="staging",
            ai_provider="openrouter",
            openrouter_api_key=SecretStr("private-or-key"),
        )
        with patch("cashly.services.openrouter_extraction.logger.info") as log:
            assert asyncio.run(module.check(settings, True)) == 0
        output = capsys.readouterr().out
        assert "private-or-key" not in output
        rows = [json.loads(line) for line in output.splitlines()]
        assert rows[0]["provider"] == "openrouter"
        assert rows[1]["outcome"] == "success"
        assert len(calls) == 1
        metadata = next(
            call.kwargs["extra"]
            for call in reversed(log.call_args_list)
            if call.args[0] == "ai_extraction_completed"
        )
        assert metadata["provider"] == "openrouter"
        assert metadata["model"] == "google/gemini-3.5-flash-lite"
        assert metadata["resolved_model"] == "synthetic/model:free"
        assert metadata["upstream_provider"] == "Synthetic Provider"
        import logging

        record = logging.LogRecord(
            "cashly.ai", logging.INFO, "", 0, "ai_extraction_completed", (), None
        )
        for key, value in metadata.items():
            setattr(record, key, value)
        for formatter in [JsonFormatter(), TerminalFormatter(color=False)]:
            formatted = formatter.format(record)
            assert (
                "synthetic/model:free" in formatted and "google/gemini-3.5-flash-lite" in formatted
            )
            assert "private-or-key" not in formatted


def test_openrouter_timeout_without_another_candidate_does_not_retry(monkeypatch):
    with provider(delay=0.2) as (url, calls):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        settings = Settings(
            environment="dev",
            ai_provider="openrouter",
            openrouter_api_key=SecretStr("private-or-key"),
        )

        async def run():
            async with httpx2.AsyncClient(trust_env=False) as client:
                original = client.post

                async def short_post(*args, **kwargs):
                    kwargs["timeout"] = httpx2.Timeout(0.05, connect=2)
                    return await original(*args, **kwargs)

                with patch.object(client, "post", short_post):
                    return await extract_chat(
                        settings,
                        client,
                        ChatExtractInput(message="kopi 18rb"),
                        {"food_drink": "Makanan"},
                        "Asia/Jakarta",
                        "test-timeout",
                    )

        with pytest.raises(ApiError) as error:
            asyncio.run(run())
        assert error.value.code == "AI_TIMEOUT"
        assert len(calls) == 1


def test_provider_configuration_is_explicit_and_secret_repr_is_hidden():
    settings = Settings.from_values(
        {
            "CASHLY_ENV": "dev",
            "AI_PROVIDER": "openrouter",
            "OPENROUTER_API_KEY": "private-or-key",
            "OPENROUTER_MODEL": "openrouter/free",
            "OPENROUTER_TIMEOUT_SECONDS": "30",
        }
    )
    assert settings.ai_provider == "openrouter"
    assert settings.openrouter_timeout_seconds == 30
    assert "private-or-key" not in repr(settings)
