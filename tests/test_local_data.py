from unittest.mock import patch

import pytest
from pydantic import SecretStr

from cashly.core.environment import ConfigurationError
from cashly.core.settings import Settings
from cashly.db.local_data import local_data, verify_local_target


def local_settings(**overrides):
    return Settings(
        **{
            "environment": "dev",
            "supabase_url": "http://127.0.0.1:54321",
            "database_url": SecretStr("postgresql://app:p@127.0.0.1:54322/postgres"),
            "migration_database_url": SecretStr("postgresql://admin:p@127.0.0.1:54322/postgres"),
            **overrides,
        }
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"environment": "prod"},
        {"environment": "staging"},
        {"supabase_url": "https://example.supabase.co"},
        {"migration_database_url": SecretStr("postgresql://u:p@db.example.com/postgres")},
        {"database_url": SecretStr("postgresql://u:p@127.0.0.1:5432/postgres")},
        {"database_url": SecretStr("postgresql://u:p@127.0.0.1:54322/other")},
        {
            "migration_database_url": SecretStr(
                "postgresql://u:p@127.0.0.1:54322/postgres?hostaddr=192.0.2.1"
            )
        },
    ],
)
def test_local_tools_reject_other_targets_before_connecting(overrides):
    with patch("cashly.db.local_data.build_engine") as build:
        with pytest.raises(ConfigurationError):
            local_data(local_settings(**overrides), "reset", confirm="cashly-local-transactions")
        build.assert_not_called()


def test_local_reset_requires_explicit_confirmation_before_connecting():
    verify_local_target(local_settings())
    with patch("cashly.db.local_data.build_engine") as build:
        with pytest.raises(ConfigurationError, match="--confirm"):
            local_data(local_settings(), "reset")
        build.assert_not_called()
