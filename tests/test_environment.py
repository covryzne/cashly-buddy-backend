from pathlib import Path
from unittest.mock import patch

import pytest

from cashly.core.environment import (
    ConfigurationError,
    branch_environment,
    detect_branch,
    resolve_environment,
)
from cashly.core.settings import Settings


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ("dev", "dev"),
        ("develop", "dev"),
        ("development", "dev"),
        ("feature/login", "dev"),
        ("stage", "staging"),
        ("staging", "staging"),
        ("main", "prod"),
        ("master", "prod"),
        ("prod", "prod"),
        ("production", "prod"),
        ("refs/heads/staging", "staging"),
    ],
)
def test_branch_mapping(branch, expected):
    assert branch_environment(branch) == expected


def test_git_branch_and_ci_precedence(tmp_path):
    with patch("cashly.core.environment.subprocess.run") as run:
        run.return_value.returncode = 0
        run.return_value.stdout = "dev\n"
        assert detect_branch(tmp_path, {}) == "dev"
        run.assert_called_once()
        run.reset_mock()
        assert detect_branch(tmp_path, {"GITHUB_REF_NAME": "staging"}) == "staging"
        run.assert_not_called()


def test_explicit_selection_and_process_precedence(tmp_path):
    (tmp_path / ".env.staging").write_text("PORT=8001\nDATABASE_URL=file-secret", encoding="utf-8")
    result = resolve_environment(
        root=tmp_path,
        override="staging",
        env={
            "CASHLY_ENV": "prod",
            "GITHUB_REF_NAME": "dev",
            "PORT": "9000",
            "DATABASE_URL": "process-secret",
        },
    )
    assert result.environment == "staging"
    assert result.values["PORT"] == "9000"
    assert result.values["DATABASE_URL"] == "process-secret"
    assert "secret" not in repr(result)


@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_missing_environment_never_falls_back_to_dev(tmp_path, environment):
    (tmp_path / ".env.dev").write_text("PORT=8000", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="missing"):
        resolve_environment(root=tmp_path, override=environment, env={})


def test_system_source_never_reads_files(tmp_path):
    with patch.object(Path, "open", side_effect=AssertionError("must not open env files")):
        result = resolve_environment(
            root=tmp_path,
            env={
                "CASHLY_ENV": "prod",
                "CASHLY_ENV_SOURCE": "system",
                "PORT": "9000",
            },
        )
    assert Settings.from_selection(result).port == 9000


@pytest.mark.parametrize(
    "variables",
    [
        {"CASHLY_ENV": "../prod"},
        {"CASHLY_ENV": "dev", "CASHLY_ENV_SOURCE": "other"},
    ],
)
def test_invalid_selector(tmp_path, variables):
    with pytest.raises(ConfigurationError):
        resolve_environment(root=tmp_path, env=variables)


@pytest.mark.parametrize(
    "variables",
    [
        {},
        {"GITHUB_REF_TYPE": "tag", "GITHUB_REF_NAME": "main"},
        {"CI_COMMIT_TAG": "release", "CI_COMMIT_REF_NAME": "main"},
        {"BUILD_SOURCEBRANCH": "refs/tags/main"},
    ],
)
def test_detached_and_tag_require_explicit_selection(tmp_path, variables):
    with patch("cashly.core.environment.subprocess.run", side_effect=OSError):
        with pytest.raises(ConfigurationError, match="No branch"):
            resolve_environment(root=tmp_path, env=variables)


def test_legacy_file_rejected_without_reading(tmp_path):
    (tmp_path / ".env").write_text("SECRET=never-read", encoding="utf-8")
    with patch.object(Path, "open", side_effect=AssertionError("must not read legacy file")):
        with pytest.raises(ConfigurationError, match="Legacy"):
            resolve_environment(root=tmp_path, override="dev", env={})


def test_dotenv_no_expansion_or_selector_override(tmp_path):
    (tmp_path / ".env.dev").write_text(
        'CASHLY_ENV=prod\nCASHLY_ENV_SOURCE=system\nDATABASE_URL="literal-${TOKEN}"\n',
        encoding="utf-8",
    )
    result = resolve_environment(root=tmp_path, override="dev", env={"TOKEN": "not-expanded"})
    assert result.values["CASHLY_ENV"] == "dev"
    assert result.values["CASHLY_ENV_SOURCE"] == "file"
    assert result.values["DATABASE_URL"] == "literal-${TOKEN}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("PORT", "secret-not-a-port"),
        ("PORT", "0"),
        ("PORT", "65536"),
        ("CORS_ORIGINS", '["*"]'),
        ("CORS_ORIGINS", "secret-not-json"),
        ("CORS_ORIGINS", '["https://example.com/path"]'),
        ("CORS_ORIGINS", '["https://user:secret@example.com"]'),
        ("LOG_LEVEL", "secret-not-a-level"),
    ],
)
def test_invalid_settings_hide_input(key, value):
    with pytest.raises(ConfigurationError) as error:
        Settings.from_values({"CASHLY_ENV": "dev", key: value})
    assert value not in str(error.value)
    assert "secret" not in str(error.value)


def test_staging_origins_require_https():
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_values({"CASHLY_ENV": "staging", "CORS_ORIGINS": '["http://example.com"]'})


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
    ],
)
def test_staging_allows_local_frontend_but_production_requires_https(origin):
    import json

    values = {"CASHLY_ENV": "staging", "CORS_ORIGINS": json.dumps([origin])}
    assert Settings.from_values(values).cors_origins == (origin,)
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_values({**values, "CASHLY_ENV": "prod"})


@pytest.mark.parametrize("origin", ["http://localhost.example.com", "http://192.168.1.10"])
def test_staging_http_exception_is_only_loopback(origin):
    import json

    with pytest.raises(ConfigurationError, match="HTTPS"):
        Settings.from_values({"CASHLY_ENV": "staging", "CORS_ORIGINS": json.dumps([origin])})


def test_settings_hide_secrets_and_allow_empty_future_connections():
    settings = Settings.from_values({"CASHLY_ENV": "dev", "DATABASE_URL": "private-secret"})
    assert "private-secret" not in repr(settings)
    assert Settings.from_values({"CASHLY_ENV": "dev", "DATABASE_URL": ""}).database_url is None
