from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from cashly.schemas.account import AccountData


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"email": "tester@example.com", "password": "Cashly-Test123!"}]
        },
    )
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: SecretStr = Field(
        min_length=8,
        max_length=16,
        description="8–16 characters: uppercase, lowercase, digit and symbol.",
    )

    @field_validator("password")
    @classmethod
    def password_composition(cls, value):
        import re

        password = value.get_secret_value()
        for pattern in (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^A-Za-z0-9\s]"):
            if not re.search(pattern, password):
                raise ValueError("Use uppercase, lowercase, number and symbol")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value):
        return value.strip() if isinstance(value, str) else value


class RegisterRequest(LoginRequest):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Tester",
                    "email": "tester@example.com",
                    "password": "Cashly-Test123!",
                }
            ]
        },
    )
    name: str = Field(min_length=2, max_length=40)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def name_without_controls(cls, value):
        if any(ord(char) < 32 for char in value):
            raise ValueError("Name contains control characters")
        return value


class EmailVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

    @field_validator("email", mode="before")
    @classmethod
    def trim_email(cls, value):
        return value.strip() if isinstance(value, str) else value


class VerifyEmailRequest(EmailVerificationRequest):
    token: SecretStr = Field(min_length=6, max_length=6)

    @field_validator("token")
    @classmethod
    def digits_only(cls, value):
        if not all(char in "0123456789" for char in value.get_secret_value()):
            raise ValueError("OTP must contain six digits")
        return value


class LinkRequest(EmailVerificationRequest):
    redirect_to: str = Field(max_length=2048)


class LinkProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_hash: SecretStr = Field(min_length=20, max_length=256)

    @field_validator("token_hash")
    @classmethod
    def valid_hash(cls, value):
        if not all(
            char.isascii() and (char.isalnum() or char in "_-") for char in value.get_secret_value()
        ):
            raise ValueError("Invalid link proof")
        return value


class PasswordRecoveryStart(LinkProof, EmailVerificationRequest):
    pass


class PasswordRecoveryProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_id: UUID
    recovery_token: SecretStr = Field(min_length=1, max_length=16384)


class PasswordRecoveryData(BaseModel):
    status: Literal["recovery_ready"] = "recovery_ready"
    recovery_id: UUID
    recovery_token: str = Field(repr=False)
    expires_at: datetime
    email: str


class PasswordRecoveryEnvelope(BaseModel):
    data: PasswordRecoveryData


class PasswordRecoveryCancelledData(BaseModel):
    status: Literal["recovery_cancelled"] = "recovery_cancelled"


class PasswordRecoveryCancelledEnvelope(BaseModel):
    data: PasswordRecoveryCancelledData


class PasswordResetVerify(PasswordRecoveryProof):
    password: SecretStr = Field(min_length=8, max_length=16)

    @field_validator("password")
    @classmethod
    def composition(cls, value):
        return LoginRequest.password_composition(value)


class PasswordResetData(BaseModel):
    status: Literal["password_reset"] = "password_reset"


class PasswordResetEnvelope(BaseModel):
    data: PasswordResetData


class AuthSession(BaseModel):
    access_token: str = Field(repr=False)
    refresh_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(gt=0)


class AuthData(BaseModel):
    status: Literal["authenticated", "registered", "verification_required", "account_pending"]
    account: AccountData | None = None
    session: AuthSession | None = Field(default=None, repr=False)


class AuthEnvelope(BaseModel):
    data: AuthData


class AuthConfiguration(BaseModel):
    supabase_url: str
    publishable_key: str
    local: bool
