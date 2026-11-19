"""Voice input/transcript contracts. Media is never a persisted expense."""

from typing import Literal

from pydantic import Field, model_validator

from cashly.schemas.extraction import ChatExtractInput, StrictModel

MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_AUDIO_ENCODED = 4 * ((MAX_AUDIO_BYTES + 2) // 3)
MAX_AUDIO_REQUEST = MAX_AUDIO_ENCODED + 8192
MAX_AUDIO_SECONDS = 60.5  # Browser encoder padding is allowed; UI records at most 60 seconds.


class VoiceAudio(StrictModel):
    mime_type: str = Field(min_length=1, max_length=80)
    data: str = Field(min_length=4, max_length=MAX_AUDIO_ENCODED)


class VoiceTranscribeInput(StrictModel):
    audio: VoiceAudio


class TranscriptResult(StrictModel):
    status: Literal["clear", "unclear"]
    transcript: str = Field(max_length=4000)
    issue: str | None = Field(max_length=240)

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "clear" and (not self.transcript or self.issue):
            raise ValueError("Clear audio requires transcript without issue")
        if self.status == "unclear" and not self.issue:
            raise ValueError("Unclear audio requires an issue")
        return self


class TranscriptData(TranscriptResult):
    duration_seconds: float


class TranscriptEnvelope(StrictModel):
    data: TranscriptData


class VoiceExtractInput(ChatExtractInput):
    message: str = Field(min_length=1, max_length=4000)
