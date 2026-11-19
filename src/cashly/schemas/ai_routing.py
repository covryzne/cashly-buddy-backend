"""Ordered provider/model targets. Credentials stay in provider-specific env variables."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FallbackModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)
    provider: Literal["gemini", "openrouter"]
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
    inputs: tuple[Literal["text", "image", "audio"], ...] = Field(min_length=1, max_length=3)
    timeout_seconds: float | None = Field(default=None, ge=1, le=40)

    @model_validator(mode="after")
    def valid_model(self):
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("Duplicate input types")
        if (
            self.provider == "gemini"
            and self.model
            and (len(self.model) > 100 or any(c in self.model for c in "/:@+"))
        ):
            raise ValueError("Gemini expects a native model ID")
        return self
