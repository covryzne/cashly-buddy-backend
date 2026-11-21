"""Decode bounded in-memory audio and normalize browser formats to mono WAV."""

import base64
import binascii
import sys
import wave
from array import array
from dataclasses import dataclass
from io import BytesIO
from time import perf_counter

import av

from cashly.core.errors import ApiError
from cashly.schemas.voice import MAX_AUDIO_BYTES, MAX_AUDIO_SECONDS, VoiceAudio

MIME_FAMILIES = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "application/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/m4a": "mp4",
    "audio/x-m4a": "mp4",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/aac": "aac",
    "audio/x-aac": "aac",
}


@dataclass(frozen=True)
class NormalizedAudio:
    data: str
    duration_seconds: float
    mime_type: str = "audio/wav"
    kind: str = "voice"


def audio_failure(
    code="INVALID_AUDIO",
    message="Audio tidak bisa dibaca. Pilih rekaman audio yang valid.",
):
    return ApiError(422, code, message)


def normalize_audio(audio: VoiceAudio):
    started = perf_counter()
    mime = audio.mime_type.split(";")[0].strip().lower()
    family = MIME_FAMILIES.get(mime)
    if not family:
        raise audio_failure(
            "UNSUPPORTED_AUDIO",
            "Gunakan audio WebM, M4A, WAV, MP3, Ogg, AAC, atau FLAC.",
        )
    try:
        raw = base64.b64decode(audio.data, validate=True)
        if len(raw) > MAX_AUDIO_BYTES:
            raise ApiError(413, "AUDIO_TOO_LARGE", "Ukuran audio maksimal 10 MB.")
        signature = (
            "wav"
            if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE"
            else "webm"
            if raw[:4] == b"\x1aE\xdf\xa3"
            else "ogg"
            if raw[:4] == b"OggS"
            else "mp4"
            if raw[4:8] == b"ftyp"
            else "flac"
            if raw[:4] == b"fLaC"
            else "mp3"
            if raw[:3] == b"ID3"
            else family
            if len(raw) >= 2
            and raw[0] == 255
            and raw[1] & 0xE0 == 0xE0
            and family in {"mp3", "aac"}
            else None
        )
        if family != signature:
            raise audio_failure()
        pcm = bytearray()
        with av.open(
            BytesIO(raw), mode="r", options={"protocol_whitelist": "file,pipe"}
        ) as container:
            if len(container.streams.audio) != 1 or container.streams.video:
                raise audio_failure(
                    "UNSUPPORTED_AUDIO", "Pilih berkas audio tanpa video, dengan satu track suara."
                )
            stream = container.streams.audio[0]
            stream.codec_context.thread_count = 1
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            samples = 0
            first_time = None

            def append(frames):
                for frame in frames:
                    pcm.extend(bytes(frame.planes[0])[: frame.samples * 2])
                    if len(pcm) / 32000 > MAX_AUDIO_SECONDS:
                        raise audio_failure("AUDIO_TOO_LONG", "Durasi audio maksimal 5 menit.")

            for frame in container.decode(stream):
                if perf_counter() - started > 5:
                    raise audio_failure(
                        "AUDIO_DECODE_TIMEOUT",
                        "Audio terlalu rumit untuk dibaca. Gunakan rekaman yang lebih singkat.",
                    )
                if not 8000 <= frame.sample_rate <= 192000 or len(frame.layout.channels) > 2:
                    raise audio_failure(
                        "UNSUPPORTED_AUDIO",
                        "Gunakan audio mono atau stereo dengan sample rate 8-192 kHz.",
                    )
                samples += frame.samples / frame.sample_rate
                if frame.time is not None:
                    if first_time is None:
                        first_time = frame.time
                    if (
                        frame.time - first_time + frame.samples / frame.sample_rate
                        > MAX_AUDIO_SECONDS
                    ):
                        raise audio_failure("AUDIO_TOO_LONG", "Durasi audio maksimal 5 menit.")
                if samples > MAX_AUDIO_SECONDS:
                    raise audio_failure("AUDIO_TOO_LONG", "Durasi audio maksimal 5 menit.")
                append(resampler.resample(frame))
            append(resampler.resample(None))
        duration = len(pcm) / 32000
        if duration < 0.25:
            raise audio_failure(
                "AUDIO_TOO_SHORT", "Rekam suara setidaknya seperempat detik, lalu coba lagi."
            )
        values = array("h", pcm)
        if sys.byteorder != "little":
            values.byteswap()
        if max(abs(value) for value in values) < 4:
            raise audio_failure(
                "AUDIO_NO_SPEECH",
                "Rekaman terdengar hening. Rekam ulang di tempat yang lebih tenang.",
            )
        output = BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(16000)
            writer.writeframes(pcm)
        return NormalizedAudio(base64.b64encode(output.getvalue()).decode(), round(duration, 3))
    except ApiError:
        raise
    except (av.FFmpegError, ValueError, OSError, EOFError, binascii.Error):
        raise audio_failure() from None
