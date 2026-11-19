"""Inspect the selected AI provider or send one synthetic expense extraction."""

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
from cashly.services.chat_extraction import extract_chat  # noqa: E402


async def check(settings, extract=False):
    selected_provider = settings.effective_ai_provider
    openrouter = selected_provider == "openrouter"
    key = settings.openrouter_api_key if openrouter else settings.gemini_api_key
    model = settings.openrouter_model if openrouter else settings.gemini_model
    print(
        json.dumps(
            {
                "environment": settings.environment,
                "requested_provider": settings.ai_provider,
                "provider": selected_provider,
                "fallback_reason": "openrouter_key_missing"
                if selected_provider != settings.ai_provider
                else None,
                "model": model,
                "key_configured": key is not None,
            }
        ),
        flush=True,
    )
    if key is None:
        print(json.dumps({"outcome": "AI_NOT_CONFIGURED"}), flush=True)
        return 1
    if not extract:
        return 0
    started = perf_counter()
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
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
    parser.add_argument("--extract", action="store_true", help="Send one synthetic extraction")
    args = parser.parse_args()
    settings = Settings.from_selection(
        resolve_environment(
            root=Path(__file__).resolve().parents[1],
            override=args.app_env,
        )
    )
    configure_logging(settings.log_level, settings.log_format, settings.log_color)
    raise SystemExit(asyncio.run(check(settings, args.extract)))


if __name__ == "__main__":
    main()
