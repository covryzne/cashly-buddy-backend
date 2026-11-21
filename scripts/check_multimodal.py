"""Probe each configured provider per modality using synthetic inputs, without DB writes."""

import argparse
import asyncio
import base64
import json
import math
import struct
import sys
import wave
from io import BytesIO
from pathlib import Path
from time import perf_counter

import httpx2
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from check_ai import receipt_input  # noqa: E402

from cashly.core.environment import resolve_environment  # noqa: E402
from cashly.core.errors import ApiError  # noqa: E402
from cashly.core.logging import configure_logging  # noqa: E402
from cashly.core.settings import Settings  # noqa: E402
from cashly.schemas.extraction import ChatExtractInput  # noqa: E402
from cashly.schemas.voice import MAX_AUDIO_BYTES, VoiceAudio  # noqa: E402
from cashly.services.chat_extraction import extract_chat  # noqa: E402
from cashly.services.receipt_extraction import extract_receipt  # noqa: E402
from cashly.services.voice_audio import normalize_audio  # noqa: E402

CATEGORIES = {"food_drink": "Makanan", "transport": "Transportasi", "other": "Lainnya"}


def total(data):
    return sum(
        sum(int(item.quantity) * int(item.priceIdr) for item in entry.draft.items)
        + int(entry.draft.taxIdr)
        - int(entry.draft.discountIdr)
        for entry in data.entries
        if entry.draft
    )


async def probe(settings, providers, audio_path=None, kinds=None):
    media = None
    if audio_path:
        if audio_path.stat().st_size > MAX_AUDIO_BYTES:
            raise ValueError("Synthetic audio exceeds 5 MB")
        mimes = {
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".mp3": "audio/mpeg",
            ".webm": "audio/webm",
            ".ogg": "audio/ogg",
        }
        media = normalize_audio(
            VoiceAudio(
                mime_type=mimes[audio_path.suffix.lower()],
                data=base64.b64encode(audio_path.read_bytes()).decode(),
            )
        )
    report = {"environment": settings.environment, "database_writes": 0, "checks": []}
    async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
        for name in providers:
            selected = settings.model_copy(update={"ai_provider": name, "ai_audio_provider": None})
            key = selected.gemini_api_key if name == "gemini" else selected.openrouter_api_key
            for kind in kinds or ["chat", "receipt", "voice"]:
                base = getattr(selected, f"{name}_model")
                override = getattr(
                    selected,
                    f"{name}_image_model" if kind.startswith("receipt") else f"{name}_audio_model",
                    None,
                )
                row = {
                    "provider": name,
                    "input_kind": kind,
                    "model": (override if kind != "chat" else None) or base,
                }
                started = perf_counter()
                try:
                    if key is None or kind == "voice" and media is None:
                        row.update(
                            outcome="not_tested",
                            reason="missing_key" if key is None else "no_audio_fixture",
                        )
                    elif kind.startswith("receipt"):
                        photo = receipt_input()
                        if kind == "receipt_unreadable":
                            source = Image.open(BytesIO(base64.b64decode(photo.image.data)))
                            output = BytesIO()
                            source.filter(ImageFilter.GaussianBlur(70)).save(output, format="PNG")
                            photo.image.data = base64.b64encode(output.getvalue()).decode()
                        data = await extract_receipt(
                            selected,
                            client,
                            photo,
                            CATEGORIES,
                            "Asia/Jakarta",
                            "synthetic-capability",
                        )
                        row.update(
                            outcome=(
                                "verified"
                                if not any(entry.draft for entry in data.entries)
                                else "needs_review"
                            )
                            if kind == "receipt_unreadable"
                            else "verified"
                            if total(data) == 12000
                            else "needs_review",
                            entry_count=len(data.entries),
                            draft_count=sum(entry.draft is not None for entry in data.entries),
                        )
                    elif kind.startswith("voice"):
                        sample = media
                        if kind == "voice_unintelligible":
                            output = BytesIO()
                            with wave.open(output, "wb") as writer:
                                writer.setnchannels(1)
                                writer.setsampwidth(2)
                                writer.setframerate(16000)
                                writer.writeframes(
                                    b"".join(
                                        struct.pack(
                                            "<h",
                                            int(4000 * math.sin(2 * math.pi * 440 * i / 16000)),
                                        )
                                        for i in range(16000)
                                    )
                                )
                            sample = normalize_audio(
                                VoiceAudio(
                                    mime_type="audio/wav",
                                    data=base64.b64encode(output.getvalue()).decode(),
                                )
                            )
                        result = await extract_chat(
                            selected,
                            client,
                            ChatExtractInput(
                                message="Transkripsikan ucapan dalam audio secara harfiah."
                            ),
                            {},
                            "UTC",
                            "synthetic-capability",
                            media=sample,
                        )
                        row.update(
                            outcome="verified"
                            if result.status
                            == ("unclear" if kind == "voice_unintelligible" else "clear")
                            else "needs_review",
                            transcript_status=result.status,
                            audio_duration_seconds=sample.duration_seconds,
                        )
                        if result.status == "clear" and kind == "voice":
                            data = await extract_chat(
                                selected,
                                client,
                                ChatExtractInput(message=result.transcript),
                                CATEGORIES,
                                "Asia/Jakarta",
                                "synthetic-capability",
                                input_kind="voice",
                            )
                            row.update(
                                entry_count=len(data.entries),
                                draft_count=sum(entry.draft is not None for entry in data.entries),
                                expected_total_matched=total(data) == 12000,
                            )
                    else:
                        data = await extract_chat(
                            selected,
                            client,
                            ChatExtractInput(message="kopi 18rb"),
                            CATEGORIES,
                            "Asia/Jakarta",
                            "synthetic-capability",
                        )
                        row.update(
                            outcome="verified" if total(data) == 18000 else "needs_review",
                            entry_count=len(data.entries),
                            draft_count=sum(entry.draft is not None for entry in data.entries),
                        )
                except ApiError as error:
                    row.update(outcome=error.code, status=error.status_code)
                row["duration_ms"] = round((perf_counter() - started) * 1000, 2)
                report["checks"].append(row)
                print(json.dumps(row), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-env", choices=["dev", "staging"], default="dev")
    parser.add_argument("--providers", nargs="+", choices=["gemini", "openrouter"])
    parser.add_argument(
        "--audio", type=Path, help="Synthetic speech fixture (never a user's private VN)"
    )
    parser.add_argument(
        "--kinds",
        nargs="+",
        choices=["chat", "receipt", "voice", "receipt_unreadable", "voice_unintelligible"],
    )
    parser.add_argument("--audio-model", help="Override only the audio model for this diagnostic")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    settings = Settings.from_selection(
        resolve_environment(root=Path(__file__).resolve().parents[1], override=args.app_env)
    )
    if args.audio_model:
        if not args.providers or len(args.providers) != 1:
            parser.error("--audio-model needs one explicit --providers value")
        settings = settings.model_copy(
            update={f"{args.providers[0]}_audio_model": args.audio_model}
        )
    configure_logging(settings.log_level, settings.log_format, settings.log_color)
    report = asyncio.run(
        probe(settings, args.providers or [settings.effective_ai_provider], args.audio, args.kinds)
    )
    if args.report:
        if args.report.is_file():
            previous = json.loads(args.report.read_text(encoding="utf-8"))
            keys = {(row["provider"], row["input_kind"]) for row in report["checks"]}
            report["checks"] = [
                row
                for row in previous.get("checks", [])
                if (row["provider"], row["input_kind"]) not in keys
            ] + report["checks"]
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
