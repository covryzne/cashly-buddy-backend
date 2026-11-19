"""OpenRouter transport; shares Cashly's draft contract and validation with Gemini."""

import asyncio
import json
import re
from datetime import UTC, datetime
from time import perf_counter
from zoneinfo import ZoneInfo

import httpx2
from pydantic import ValidationError

from cashly.core.errors import ApiError
from cashly.schemas.chat import ModelExtraction
from cashly.services.chat_extraction import SYSTEM_PROMPT, apply_extraction, invalid_result, logger

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def provider_failure(status):
    if status in {401, 403}:
        return ApiError(503, "AI_ACCESS_DENIED", "Akses OpenRouter ditolak. Periksa key di server.")
    if status == 402:
        return ApiError(
            503, "AI_CREDITS_REQUIRED", "Kredit atau akses model OpenRouter belum tersedia."
        )
    if status == 404:
        return ApiError(503, "AI_MODEL_NOT_FOUND", "Model atau endpoint OpenRouter belum tersedia.")
    if status in {400, 422}:
        return ApiError(
            502, "AI_REQUEST_REJECTED", "Model OpenRouter belum menerima format ekstraksi."
        )
    if status == 429:
        return ApiError(429, "AI_RATE_LIMITED", "Kuota OpenRouter sedang penuh. Coba lagi nanti.")
    if status in {408, 504}:
        return ApiError(504, "AI_TIMEOUT", "OpenRouter belum merespons. Coba kirim ulang pesanmu.")
    return ApiError(503, "AI_UNAVAILABLE", "OpenRouter belum tersedia. Pesan dan draft tetap aman.")


async def extract_openrouter_chat(settings, client, body, categories, timezone, request_id):
    if settings.openrouter_api_key is None:
        raise ApiError(
            503, "AI_NOT_CONFIGURED", "API key OpenRouter belum dikonfigurasi di server."
        )
    now = datetime.now(UTC).astimezone(ZoneInfo(timezone))
    context = {
        **body.model_dump(mode="json"),
        "current_time": now.isoformat(),
        "timezone": timezone,
        "categories": categories,
    }
    started = perf_counter()
    outcome = "failed"
    extra = {
        "request_id": request_id,
        "provider": "openrouter",
        "model": settings.openrouter_model,
        "attempt": 1,
        "transport": "httpx2",
        "api_version": "v1",
    }
    logger.info("ai_extraction_started", extra=extra)
    try:
        # A per-read timeout does not bound a slow trickle of bytes, so enforce a hard
        # overall deadline for the whole provider call.
        async with asyncio.timeout(settings.openrouter_timeout_seconds):
            response = await client.post(
                f"{OPENROUTER_BASE_URL}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openrouter_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                    "X-Title": "Cashly",
                },
                json={
                    "model": settings.openrouter_model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps(context)},
                    ],
                    "response_format": {
                        "type": "json_schema",
                        "json_schema": {
                            "name": "cashly_expense_extraction",
                            "strict": True,
                            "schema": ModelExtraction.model_json_schema(),
                        },
                    },
                    "provider": {"require_parameters": True},
                    # OpenRouter rejects a request whose max_tokens exceeds the affordable
                    # budget (HTTP 402). Extraction output is small, so keep the cap modest.
                    "max_tokens": 4096,
                    "stream": False,
                },
                timeout=httpx2.Timeout(
                    settings.openrouter_timeout_seconds, connect=8, write=10, pool=5
                ),
            )
        extra["upstream_status"] = response.status_code
        if response.status_code != 200:
            outcome = "rate_limited" if response.status_code == 429 else "upstream_error"
            raise provider_failure(response.status_code)
        if len(response.content) > 262144:
            outcome = "invalid_result"
            raise invalid_result()
        envelope = response.json()
        error = envelope.get("error")
        if error is not None:
            # Some upstream failures are wrapped in HTTP 200. Never treat them as a draft.
            status = error.get("code") if isinstance(error, dict) else None
            status = status if isinstance(status, int) and 400 <= status <= 599 else 503
            extra["provider_error_code"] = status
            outcome = "rate_limited" if status == 429 else "upstream_error"
            raise provider_failure(status)
        for field, target in [("model", "resolved_model"), ("provider", "upstream_provider")]:
            value = envelope.get(field)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 ._:/@+-]{1,200}", value):
                extra[target] = value
        choice = envelope["choices"][0]
        if choice.get("finish_reason") != "stop" or choice.get("error"):
            outcome = "invalid_result"
            raise invalid_result()
        message = choice["message"]
        raw = message.get("content")
        if message.get("refusal") or not isinstance(raw, str):
            outcome = "invalid_result"
            raise invalid_result()
        result = ModelExtraction.model_validate_json(raw)
        data = apply_extraction(body, result, categories, now)
        outcome = result.intent
        return data
    except TimeoutError:
        outcome = "timeout"
        extra["exception_type"] = "TimeoutError"
        raise provider_failure(504) from None
    except httpx2.TimeoutException as exc:
        outcome = "timeout"
        extra["exception_type"] = type(exc).__name__
        raise provider_failure(504) from None
    except httpx2.HTTPError as exc:
        outcome = "connection_error"
        extra["exception_type"] = type(exc).__name__
        raise provider_failure(503) from None
    except (ValidationError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        outcome = "invalid_result"
        raise invalid_result() from None
    finally:
        logger.info(
            "ai_extraction_completed",
            extra={
                **extra,
                "outcome": outcome,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
