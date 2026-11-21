"""Decoded browser audio, real loopback HTTP, and transcript-before-extraction."""

import base64
import json
import math
import struct
import wave
from io import BytesIO

import av
import pytest
from pydantic import SecretStr
from test_chat_extraction import extraction, provider
from test_openrouter_extraction import provider as router_provider
from test_receipt_extraction import client_for

from cashly.core.errors import ApiError
from cashly.schemas.voice import VoiceAudio
from cashly.services.voice_audio import normalize_audio


@pytest.fixture(autouse=True)
def active_test_account(monkeypatch):
    # HTTP contract fixtures use no real DB; live E2E exercises the actual account check.
    monkeypatch.setattr("cashly.api.chat.ensure_extraction_account", lambda *_: None)


def wav_audio(seconds=1, silent=False):
    output = BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(
            b"".join(
                struct.pack(
                    "<h", 0 if silent else int(4000 * math.sin(2 * math.pi * 440 * i / 16000))
                )
                for i in range(int(seconds * 16000))
            )
        )
    return output.getvalue()


def encoded_audio(format="wav", seconds=1, silent=False):
    raw = wav_audio(seconds, silent)
    formats = {
        "webm": ("webm", "libopus", 48000, "audio/webm;codecs=opus"),
        "mp4": ("mp4", "aac", 16000, "audio/mp4"),
        "ogg": ("ogg", "libopus", 48000, "audio/ogg"),
        "mp3": ("mp3", "libmp3lame", 16000, "audio/mpeg"),
        "flac": ("flac", "flac", 16000, "audio/flac"),
        "aac": ("adts", "aac", 16000, "audio/aac"),
    }
    mime = "audio/wav"
    if format != "wav":
        container_format, codec, rate, mime = formats[format]
        output = BytesIO()
        with (
            av.open(BytesIO(raw)) as source,
            av.open(output, "w", format=container_format) as target,
        ):
            stream = target.add_stream(codec, rate=rate)
            stream.layout = "mono"
            resample = av.AudioResampler(
                format=stream.codec_context.format.name, layout="mono", rate=rate
            )
            for frame in source.decode(audio=0):
                for converted in resample.resample(frame):
                    for packet in stream.encode(converted):
                        target.mux(packet)
            for converted in resample.resample(None):
                for packet in stream.encode(converted):
                    target.mux(packet)
            for packet in stream.encode(None):
                target.mux(packet)
        raw = output.getvalue()
    return {"mime_type": mime, "data": base64.b64encode(raw).decode()}


def transcript(status="clear", text="Hari ini beli kopi delapan belas ribu qris", issue=None):
    return {"status": status, "transcript": text, "issue": issue}


def gemini_response(data):
    return {
        "status": "completed",
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": json.dumps(data)}]}
        ],
    }


@pytest.mark.parametrize("format", ["wav", "webm", "mp4", "ogg", "mp3", "aac", "flac"])
def test_browser_formats_decode_to_bounded_wav(format):
    audio = normalize_audio(VoiceAudio(**encoded_audio(format)))
    assert 0.9 <= audio.duration_seconds <= 1.2
    with wave.open(BytesIO(base64.b64decode(audio.data))) as normalized:
        assert (
            normalized.getnchannels(),
            normalized.getsampwidth(),
            normalized.getframerate(),
        ) == (1, 2, 16000)


def test_audio_longer_than_one_minute_can_be_transcribed(monkeypatch):
    with provider(content=gemini_response(transcript())) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post(
                "/api/v1/voice/transcribe", json={"audio": encoded_audio(seconds=61)}
            )
    assert response.status_code == 200
    assert response.json()["data"]["duration_seconds"] == 61
    assert len(calls) == 1


@pytest.mark.parametrize(
    "seconds,silent,code",
    [(301, False, "AUDIO_TOO_LONG"), (0.1, False, "AUDIO_TOO_SHORT"), (1, True, "AUDIO_NO_SPEECH")],
)
def test_real_decoded_duration_and_silence(seconds, silent, code):
    with pytest.raises(ApiError) as error:
        normalize_audio(VoiceAudio(**encoded_audio(seconds=seconds, silent=silent)))
    assert error.value.code == code


@pytest.mark.parametrize("bad", ["mime", "corrupt", "mismatch", "base64", "size"])
def test_invalid_audio_does_not_reach_provider(monkeypatch, bad):
    audio = encoded_audio()
    if bad == "mime":
        audio["mime_type"] = "application/pdf"
    elif bad == "corrupt":
        audio["data"] = base64.b64encode(b"RIFF0000WAVEbroken").decode()
    elif bad == "mismatch":
        audio["mime_type"] = "audio/mp4"
    elif bad == "base64":
        audio["data"] = "%%%%"
    else:
        monkeypatch.setattr("cashly.api.chat.MAX_AUDIO_REQUEST", 100)
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": audio})
    assert response.status_code == (413 if bad == "size" else 422)
    assert not calls
    assert audio["data"] not in response.text


def test_transcription_returns_actual_response_then_extracts_reviewed_text(monkeypatch):
    with provider(content=gemini_response(transcript())) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(
            monkeypatch, gemini_api_key=SecretStr("fake"), gemini_audio_model="audio-test"
        ) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio("mp4")})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["transcript"] == transcript()["transcript"]
    request = calls[0]["body"]
    assert request["model"] == "audio-test"
    assert request["input"][1]["type"] == "audio"
    assert request["input"][1]["mime_type"] == "audio/wav"
    assert request["store"] is False
    assert "No transaction extraction" in request["system_instruction"]
    with provider(content=gemini_response(extraction())) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(
            monkeypatch,
            gemini_api_key=SecretStr("fake"),
            gemini_audio_model="audio-test",
            gemini_model="text-test",
        ) as client:
            response = client.post("/api/v1/voice/extract", json={"message": "kopi 18rb qris"})
    assert response.status_code == 200, response.text
    assert calls[0]["body"]["model"] == "text-test"
    assert json.loads(calls[0]["body"]["input"])["message"] == "kopi 18rb qris"
    assert response.json()["data"]["entries"][0]["draft"]["items"][0]["priceIdr"] == "18000"


def test_openrouter_receives_normalized_audio_and_model_override(monkeypatch):
    envelope = {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(transcript())}}]
    }
    with router_provider(content=envelope) as (url, calls):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        with client_for(
            monkeypatch,
            ai_provider="openrouter",
            openrouter_api_key=SecretStr("fake"),
            openrouter_audio_model="audio/model",
        ) as client:
            response = client.post(
                "/api/v1/voice/transcribe", json={"audio": encoded_audio("webm")}
            )
    assert response.status_code == 200, response.text
    request = calls[0]["body"]
    assert request["model"] == "audio/model"
    assert "response_format" not in request
    assert "Required JSON schema" in request["messages"][0]["content"]
    assert request["messages"][1]["content"][1]["input_audio"]["format"] == "wav"


@pytest.mark.parametrize(
    "result,expected",
    [
        (transcript("unclear", "", "Suara belum jelas. Perbaiki transkrip."), 200),
        ({"status": "clear", "transcript": "", "issue": None}, 502),
        ({"status": "clear", "transcript": "x", "issue": "should not exist"}, 502),
    ],
)
def test_unclear_or_invalid_transcription_never_creates_a_draft(monkeypatch, result, expected):
    with provider(content=gemini_response(result)) as (url, _):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio()})
    assert response.status_code == expected
    assert "draft" not in response.json().get("data", {})


def test_timeout_releases_inflight_slot_and_logs_no_audio(monkeypatch, caplog):
    with provider(content=gemini_response(transcript()), delay=0.25) as (url, _):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            client.app.state.settings = client.app.state.settings.model_copy(
                update={"gemini_timeout_seconds": 0.03}
            )
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio()})
            assert response.status_code == 504
            assert response.json()["error"]["message"].startswith(
                "Mochi belum bisa memproses catatan suaramu."
            )
            assert all(not busy for _, busy in client.app.state.chat_limits.values())
    assert "RIFF" not in caplog.text and transcript()["transcript"] not in caplog.text


def test_audio_provider_override_uses_gemini_without_changing_chat_provider(monkeypatch):
    with provider(content=gemini_response(transcript())) as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(
            monkeypatch,
            ai_provider="openrouter",
            ai_audio_provider="gemini",
            openrouter_api_key=SecretStr("unused"),
            gemini_api_key=SecretStr("fake"),
        ) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio()})
    assert response.status_code == 200
    assert len(calls) == 1 and calls[0]["path"] == "/interactions"


def test_audio_unsupported_message_is_sanitized(monkeypatch):
    with router_provider(
        status=404,
        content={
            "error": {"message": "No endpoints support audio input. Private token never-return"}
        },
    ) as (url, _):
        monkeypatch.setattr("cashly.services.openrouter_extraction.OPENROUTER_BASE_URL", url)
        with client_for(
            monkeypatch, ai_provider="openrouter", openrouter_api_key=SecretStr("fake")
        ) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio()})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AI_INPUT_UNSUPPORTED"
    assert "never-return" not in response.text


def test_inactive_account_does_not_reach_provider(monkeypatch):
    def deny(*_):
        raise ApiError(403, "ACCOUNT_INACTIVE", "Akun belum aktif.")

    monkeypatch.setattr("cashly.api.chat.ensure_extraction_account", deny)
    with provider() as (url, calls):
        monkeypatch.setattr("cashly.services.chat_extraction.GEMINI_BASE_URL", url)
        with client_for(monkeypatch, gemini_api_key=SecretStr("fake")) as client:
            response = client.post("/api/v1/voice/transcribe", json={"audio": encoded_audio()})
    assert response.status_code == 403
    assert not calls
