"""One bounded application-owned fallback chain for text, OCR and voice."""

import asyncio
from datetime import UTC, datetime
from time import monotonic, perf_counter
from zoneinfo import ZoneInfo

from cashly.core.errors import ApiError
from cashly.services.chat_extraction import extract_gemini_chat, logger
from cashly.services.openrouter_extraction import extract_openrouter_chat

# Add a transport adapter and credentials/settings when supporting a new provider.
ADAPTERS = {"gemini": extract_gemini_chat, "openrouter": extract_openrouter_chat}
FALLBACK_ERRORS = {
    "AI_TIMEOUT",
    "AI_UNAVAILABLE",
    "AI_RATE_LIMITED",
    "AI_MODEL_NOT_FOUND",
    "AI_INPUT_UNSUPPORTED",
    "AI_REQUEST_REJECTED",
    "AI_ACCESS_DENIED",
    "AI_CREDITS_REQUIRED",
    "AI_INVALID_RESULT",
    "AI_NOT_CONFIGURED",
}


def model_for(settings, provider, modality):
    override = (
        getattr(settings, f"{provider}_{'audio' if modality == 'audio' else 'image'}_model")
        if modality != "text"
        else None
    )
    return override or getattr(settings, f"{provider}_model")


def selected_settings(settings, provider, modality, model, timeout):
    values = {"ai_provider": provider, f"{provider}_timeout_seconds": timeout}
    values[
        f"{provider}_{'audio' if modality == 'audio' else 'image'}_model"
        if modality != "text"
        else f"{provider}_model"
    ] = model
    return settings.model_copy(update=values)


async def extract_with_fallback(
    settings, client, body, categories, timezone, request_id, media=None, input_kind=None
):
    now = datetime.now(UTC).astimezone(ZoneInfo(timezone))
    audio = media is not None and getattr(media, "kind", None) == "voice"
    modality = "audio" if audio else "image" if media else "text"
    kind = "voice" if audio else "receipt" if media else input_kind or "chat"
    phase = "transcribe" if audio else "extract"
    requested = (settings.ai_audio_provider if audio else None) or settings.ai_provider
    primary = (
        "gemini" if requested == "openrouter" and settings.openrouter_api_key is None else requested
    )
    if primary != requested:
        logger.info(
            "ai_provider_fallback",
            extra={
                "request_id": request_id,
                "requested_provider": requested,
                "provider": primary,
                "model": model_for(settings, primary, modality),
                "input_kind": kind,
                "phase": phase,
                "fallback_reason": "openrouter_key_missing",
            },
        )
    candidates = [(primary, model_for(settings, primary, modality), None)]
    candidates.extend(
        (
            target.provider,
            target.model or model_for(settings, target.provider, modality),
            target.timeout_seconds,
        )
        for target in settings.ai_fallback_models
        if modality in target.inputs
    )
    unique = []
    seen = set()
    for provider, model, timeout in candidates:
        if (provider, model) not in seen:
            seen.add((provider, model))
            unique.append((provider, model, timeout))
    started = perf_counter()
    deadline = monotonic() + settings.ai_total_timeout_seconds
    last_error = None
    previous = None
    attempted = 0
    outcome = "exhausted"
    try:
        for index, (provider, model, configured_timeout) in enumerate(unique, 1):
            remaining = deadline - monotonic()
            if remaining <= 0.01:
                outcome = "timeout"
                raise ApiError(
                    504,
                    "AI_TIMEOUT",
                    "Prosesnya lebih lama dari biasanya. Coba lagi sebentar, ya.",
                )
            extra = {
                "request_id": request_id,
                "provider": provider,
                "model": model,
                "input_kind": kind,
                "phase": phase,
                "attempt": index,
            }
            if getattr(settings, f"{provider}_api_key") is None:
                logger.info(
                    "ai_provider_skipped", extra={**extra, "fallback_reason": "api_key_missing"}
                )
                continue
            if previous is not None:
                logger.info(
                    "ai_provider_fallback",
                    extra={
                        **extra,
                        "from_provider": previous[0],
                        "from_model": previous[1],
                        "fallback_reason": last_error.code,
                    },
                )
            timeout = min(
                remaining,
                settings.ai_attempt_timeout_seconds,
                configured_timeout or getattr(settings, f"{provider}_timeout_seconds"),
            )
            active = selected_settings(settings, provider, modality, model, timeout)
            attempted += 1
            try:
                async with asyncio.timeout(remaining):
                    # Resolve the Gemini adapter from its owning module for compatibility
                    # with existing direct transport instrumentation.
                    if provider == "gemini":
                        from cashly.services import chat_extraction

                        adapter = chat_extraction.extract_gemini_chat
                    else:
                        adapter = ADAPTERS[provider]
                    result = await adapter(
                        active,
                        client,
                        body,
                        categories,
                        timezone,
                        request_id,
                        media,
                        input_kind,
                        attempt=index,
                        now=now,
                    )
                outcome = "success"
                return result
            except TimeoutError:
                outcome = "timeout"
                raise ApiError(
                    504,
                    "AI_TIMEOUT",
                    "Prosesnya lebih lama dari biasanya. Coba lagi sebentar, ya.",
                ) from None
            except ApiError as error:
                if error.code not in FALLBACK_ERRORS:
                    outcome = "rejected"
                    raise
                last_error = error
                previous = (provider, model)
        if last_error is not None:
            raise last_error
        raise ApiError(
            503, "AI_NOT_CONFIGURED", "Mochi belum bisa memprosesnya. Coba lagi sebentar, ya."
        )
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        logger.info(
            "ai_extraction_chain_completed",
            extra={
                "request_id": request_id,
                "input_kind": kind,
                "phase": phase,
                "attempt_count": attempted,
                "outcome": outcome,
                "duration_ms": round((perf_counter() - started) * 1000, 2),
            },
        )
