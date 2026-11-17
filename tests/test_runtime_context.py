import pytest
from pydantic import SecretStr

from cashly.core.settings import Settings
from cashly.services.accounts import runtime_context


@pytest.mark.parametrize(
    "environment,host,expected",
    [
        ("dev", "127.0.0.1", "dev"),
        ("staging", "localhost", "dev"),
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


@pytest.mark.parametrize(
    "environment,host,expected",
    [
        ("dev", "127.0.0.1", True),
        ("staging", "127.0.0.1", False),
        ("prod", "127.0.0.1", False),
        ("dev", "db.example.supabase.co", False),
    ],
)
def test_weekly_override_only_local_development(environment, host, expected):
    from cashly.services.gamification import weekly_test_override

    settings = Settings(
        environment=environment,
        weekly_review_test_mode=True,
        database_url=SecretStr(f"postgresql://u:p@{host}/postgres"),
    )
    assert weekly_test_override(settings) is expected
