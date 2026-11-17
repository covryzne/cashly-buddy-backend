from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from cashly.schemas.auth import EmailVerificationRequest, LoginRequest, VerifyEmailRequest


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=2, max_length=40)
    username: str = Field(min_length=3, max_length=24, pattern=r"^[a-z0-9._]+$")

    @field_validator("display_name", "username", mode="before")
    @classmethod
    def normalize(cls, value, info):
        if isinstance(value, str):
            value = value.strip()
            return value.lower() if info.field_name == "username" else value
        return value

    @field_validator("display_name")
    @classmethod
    def no_controls(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError("Name contains control characters")
        return value


class EmptyAction(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmailChangeInput(EmailVerificationRequest):
    pass


class SecurityCodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_id: UUID
    token: SecretStr = Field(min_length=6, max_length=6)

    @field_validator("token")
    @classmethod
    def digits_only(cls, value):
        return VerifyEmailRequest.digits_only(value)


class EmailChangeVerify(SecurityCodeInput):
    address: Literal["current", "new"]


class PasswordChangeVerify(SecurityCodeInput):
    password: SecretStr = Field(min_length=8, max_length=16)

    @field_validator("password")
    @classmethod
    def composition(cls, value):
        return LoginRequest.password_composition(value)


class SecurityChallengeData(BaseModel):
    status: Literal["verification_required"]
    challenge_id: UUID
    expires_at: datetime
    current_email: str
    new_email: str | None


class SecurityChallengeEnvelope(BaseModel):
    data: SecurityChallengeData


class SecurityResultData(BaseModel):
    status: Literal["confirmation_pending", "email_changed", "password_changed"]
    confirmed_current: bool = False
    confirmed_new: bool = False


class SecurityResultEnvelope(BaseModel):
    data: SecurityResultData
