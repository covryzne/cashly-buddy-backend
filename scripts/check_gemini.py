"""Safe provider diagnostic: synthetic inputs only, never prints keys or response bodies."""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cashly.core.environment import resolve_environment  # noqa: E402
from cashly.core.errors import ApiError  # noqa: E402
from cashly.core.logging import configure_logging  # noqa: E402
from cashly.core.settings import Settings  # noqa: E402
from cashly.schemas.chat import ChatExtractInput  # noqa: E402
from cashly.services.chat_extraction import (  # noqa: E402
    GEMINI_BASE_URL,
    extract_chat,
    interaction_output_text,
    safe_provider_error,
)


async def basic_request(settings, sdk, api_version):
    """No schema, history, system prompt or thinking override: isolate basic generation."""
    started = perf_counter()
    metadata = {
        "check": "sdk_basic" if sdk else "rest_basic",
        "api_version": api_version,
        "endpoint": "interactions",
    }
    try:
        if sdk:
            try:
                from google import genai
                from google.genai import types
            except ImportError:
                print(json.dumps({**metadata, "outcome": "SDK_NOT_INSTALLED"}))
                return 1
            with genai.Client(
                api_key=settings.gemini_api_key.get_secret_value(),
                enterprise=False,
                http_options=types.HttpOptions(
                    api_version=api_version,
                    base_url=GEMINI_BASE_URL.rsplit("/", 1)[0],
                    timeout=settings.gemini_timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            ) as provider:
                async with provider.aio as client:
                    response = await client.interactions.create(
                        model=settings.gemini_model,
                        input="Reply only OK.",
                        store=False,
                    )
                    metadata.update(
                        outcome="success" if response.status == "completed" else "incomplete",
                        has_output=bool(response.output_text),
                        status=200,
                    )
        else:
            async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
                response = await client.post(
                    f"{GEMINI_BASE_URL.rsplit('/', 1)[0]}/{api_version}/interactions",
                    headers={"x-goog-api-key": settings.gemini_api_key.get_secret_value()},
                    json={
                        "model": settings.gemini_model,
                        "input": "Reply only OK.",
                        "store": False,
                    },
                    timeout=httpx2.Timeout(settings.gemini_timeout_seconds, connect=8),
                )
                metadata["status"] = response.status_code
                if response.status_code != 200:
                    metadata.update(safe_provider_error(response))
                    metadata["outcome"] = "upstream_error"
                else:
                    metadata.update(
                        outcome="success", has_output=bool(interaction_output_text(response.json()))
                    )
    except Exception as exc:
        # SDK exception strings can contain request/response data. Print only finite metadata.
        metadata.update(outcome="failed", exception_type=type(exc).__name__)
        response = getattr(exc, "response", None)
        code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        if code is None and response is not None:
            code = getattr(response, "status_code", None)
        if isinstance(code, int) and 400 <= code <= 599:
            metadata["status"] = code
        if response is not None:
            metadata.update(safe_provider_error(response))
    metadata["duration_ms"] = round((perf_counter() - started) * 1000, 2)
    print(json.dumps(metadata))
    return 0 if metadata.get("outcome") == "success" else 1


async def check(settings, extract, basic=False, sdk_basic=False, api_version=None):
    print(
        json.dumps(
            {
                "provider": "gemini",
                "model": settings.gemini_model,
                "thinking_level": "model_default"
                if basic or sdk_basic
                else settings.gemini_thinking_level,
                "key_configured": settings.gemini_api_key is not None,
            }
        )
    )
    if settings.gemini_api_key is None:
        return 1
    if basic or sdk_basic:
        return await basic_request(settings, sdk_basic, api_version or "v1beta")
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
        started = perf_counter()
        try:
            response = await client.get(
                f"{GEMINI_BASE_URL}/models/{settings.gemini_model}",
                headers={"x-goog-api-key": settings.gemini_api_key.get_secret_value()},
                timeout=httpx2.Timeout(12, connect=8),
            )
            print(
                json.dumps(
                    {
                        "check": "model_metadata",
                        "status": response.status_code,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    }
                )
            )
            if response.status_code != 200:
                return 1
        except httpx2.HTTPError as exc:
            print(json.dumps({"check": "model_metadata", "exception_type": type(exc).__name__}))
            return 1
        if not extract:
            return 0
        # This costs one provider request. The flag is explicit and input is always fictional.
        started = perf_counter()
        try:
            data = await extract_chat(
                settings,
                client,
                ChatExtractInput(message="kopi 18rb"),
                {"food_drink": "Makanan", "transport": "Transportasi", "other": "Lainnya"},
                "Asia/Jakarta",
                "synthetic-diagnostic",
            )
            print(
                json.dumps(
                    {
                        "check": "synthetic_extraction",
                        "outcome": "success",
                        "draft_count": len(data.entries),
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    }
                )
            )
            return 0
        except ApiError as exc:
            print(
                json.dumps(
                    {
                        "check": "synthetic_extraction",
                        "outcome": exc.code,
                        "status": exc.status_code,
                        "duration_ms": round((perf_counter() - started) * 1000, 2),
                    }
                )
            )
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-env", choices=["dev", "staging", "prod"], default="dev")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--extract", action="store_true", help="Send one synthetic extraction to Gemini"
    )
    mode.add_argument(
        "--basic", action="store_true", help="One REST generation without extraction schema"
    )
    mode.add_argument(
        "--sdk-basic",
        action="store_true",
        help="One official SDK generation; requires google-genai",
    )
    parser.add_argument(
        "--api-version",
        choices=["v1", "v1beta"],
        default=None,
        help="Basic Interactions API version (default v1beta); app always uses v1beta",
    )
    args = parser.parse_args()
    settings = Settings.from_selection(
        resolve_environment(
            root=Path(__file__).resolve().parents[1],
            override=args.app_env,
        )
    )
    configure_logging(settings.log_level, settings.log_format, settings.log_color)
    # This legacy diagnostic is explicitly Gemini-only. check_ai.py follows AI_PROVIDER.
    settings = settings.model_copy(update={"ai_provider": "gemini"})
    raise SystemExit(
        asyncio.run(
            check(
                settings,
                args.extract,
                args.basic,
                args.sdk_basic,
                args.api_version,
            )
        )
    )


if __name__ == "__main__":
    main()
