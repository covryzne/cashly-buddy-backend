import pytest
from pydantic import SecretStr

from cashly.core.settings import Settings
from cashly.services.accounts import runtime_context


@pytest.mark.parametrize(
    "environment,host,expected",
    [
        ("dev", "127.0.0.1", "dev"),
        ("staging", "localhost", "staging"),
        ("staging", "db.example.supabase.co", "staging"),
        ("prod", "db.example.supabase.co", "prod"),
        ("dev", "db.example.supabase.co", "dev"),
    ],
)
def test_runtime_metadata_uses_database_target_without_secrets(environment, host, expected):
    settings = Settings(
        environment=environment,
        database_url=SecretStr(
            f"postgresql://private_user:private_password@{host}:5432/private_database"
        ),
    )
    result = runtime_context(settings).model_dump()
    assert result == {
        "environment": expected,
        "database_kind": "local" if host in {"127.0.0.1", "localhost"} else "hosted",
    }
    assert "private" not in str(result) and "supabase" not in str(result)


def test_retired_weekly_action_is_rejected_before_database_access():
    from cashly.core.errors import ApiError
    from cashly.services.gamification import action

    with pytest.raises(ApiError) as error:
        action(None, None, "Asia/Jakarta", "weekly")
    assert error.value.status_code == 404
