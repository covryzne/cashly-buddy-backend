import pytest
from pydantic import ValidationError

from cashly.schemas.auth import LoginRequest, RegisterRequest


@pytest.mark.parametrize("model", [LoginRequest, RegisterRequest])
@pytest.mark.parametrize(
    "password",
    [
        "Aa1!",
        "lowercase123!",
        "UPPERCASE123!",
        "NoDigitsHere!",
        "NoSymbols123",
        "A" * 16 + "a1!",
    ],
)
def test_password_policy_rejects_missing_requirements(model, password):
    body = {"email": "tester@example.com", "password": password}
    if model is RegisterRequest:
        body["name"] = "Tester"
    with pytest.raises(ValidationError):
        model(**body)


@pytest.mark.parametrize("model", [LoginRequest, RegisterRequest])
@pytest.mark.parametrize("password", ["Ab1!xxxx", "Ab1!xxxxxxxxxxxx", " Ab1!xxx"])
def test_password_policy_accepts_composition_and_does_not_trim_password(model, password):
    body = {"email": "tester@example.com", "password": password}
    if model is RegisterRequest:
        body["name"] = "Tester"
    result = model(**body)
    assert result.password.get_secret_value() == body["password"]
