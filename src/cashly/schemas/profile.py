from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from cashly.schemas.auth import LoginRequest


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


class AccountDeletionInput(EmptyAction):
    confirmation: Literal["HAPUS AKUN"]
    current_password: SecretStr | None = Field(default=None, min_length=1, max_length=128)


class AccountDeletionData(BaseModel):
    status: Literal["account_deleted"] = "account_deleted"


class AccountDeletionEnvelope(BaseModel):
    data: AccountDeletionData


class PasswordUpdate(EmptyAction):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=8, max_length=16)

    @field_validator("password")
    @classmethod
    def composition(cls, value):
        return LoginRequest.password_composition(value)


class PasswordSetupRequest(EmptyAction):
    redirect_to: str = Field(max_length=2048)


class SecurityResultData(BaseModel):
    status: Literal["link_sent", "password_changed"]


class SecurityResultEnvelope(BaseModel):
    data: SecurityResultData


class GoogleConnectionData(BaseModel):
    connected: bool
    google_email: str | None
    password_login_enabled: bool
    can_disconnect: bool


class GoogleConnectionEnvelope(BaseModel):
    data: GoogleConnectionData
