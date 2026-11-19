from unittest.mock import patch

import pytest

from cashly.cli import main
from cashly.core.environment import EnvironmentSelection, resolve_environment


def selection(environment="dev"):
    return EnvironmentSelection(
        environment,
        "system",
        f".env.{environment}",
        {
            "CASHLY_ENV": environment,
            "CASHLY_ENV_SOURCE": "system",
            "DATABASE_URL": "private-secret",
        },
    )


def test_check_prints_only_metadata(capsys):
    with (
        patch("sys.argv", ["cashly", "check", "--app-env", "dev"]),
        patch("cashly.cli.resolve_environment", return_value=selection()),
        patch("cashly.cli.uvicorn.run") as run,
    ):
        main()
    run.assert_not_called()
    assert "private-secret" not in capsys.readouterr().out


def test_database_check_prints_safe_result(capsys):
    with (
        patch("sys.argv", ["cashly", "db-check"]),
        patch("cashly.cli.resolve_environment", return_value=selection()),
        patch("cashly.cli.check_database", return_value={"connected": True}),
    ):
        main()
    assert '"connected": true' in capsys.readouterr().out


def test_sql_preview_never_resolves_environment(capsys):
    with (
        patch("sys.argv", ["cashly", "db-sql"]),
        patch("cashly.cli.resolve_environment", side_effect=AssertionError),
        patch("cashly.cli.render_migration_sql", return_value="-- safe offline SQL"),
    ):
        main()
    assert "offline SQL" in capsys.readouterr().out


def test_reload_rejected_for_prod():
    with (
        patch("sys.argv", ["cashly", "dev", "--app-env", "prod"]),
        patch("cashly.cli.resolve_environment", return_value=selection("prod")),
        patch("cashly.cli.uvicorn.run") as run,
    ):
        with pytest.raises(SystemExit) as error:
            main()
    assert error.value.code == 2
    run.assert_not_called()


def test_start_passes_pinned_selection_to_children(monkeypatch):
    monkeypatch.delenv("CASHLY_ENV", raising=False)
    monkeypatch.delenv("CASHLY_ENV_SOURCE", raising=False)
    with (
        patch("sys.argv", ["cashly", "start"]),
        patch("cashly.cli.resolve_environment", return_value=selection("staging")),
        patch("cashly.cli.uvicorn.run") as run,
    ):
        main()
    assert run.call_args.kwargs["reload"] is False
    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["proxy_headers"] is False
    import os

    assert os.environ["CASHLY_ENV"] == "staging"
    assert os.environ["CASHLY_ENV_SOURCE"] == "system"


@pytest.mark.parametrize("flag", ["--env", "--app-env"])
def test_local_reload_can_select_staging_file(flag, tmp_path, monkeypatch, capsys):
    import os

    monkeypatch.setenv("CASHLY_ENV", "dev")
    monkeypatch.setenv("CASHLY_ENV_SOURCE", "system")
    (tmp_path / ".env.staging").write_text(
        "CORS_ORIGINS='[\"http://localhost:3000\"]'\n"
        "SUPABASE_PUBLISHABLE_KEY=private-settings-key\n",
        encoding="utf-8",
    )
    with (
        patch("sys.argv", ["cashly", "dev", flag, "staging", "--env-source", "file"]),
        patch(
            "cashly.cli.resolve_environment",
            side_effect=lambda **kwargs: resolve_environment(root=tmp_path, env={}, **kwargs),
        ),
        patch("cashly.cli.uvicorn.run") as run,
    ):
        main()
    output = capsys.readouterr().out
    assert "environment=staging source=file file=.env.staging" in output
    assert "private-settings-key" not in output
    assert run.call_args.kwargs["reload"] is True
    assert os.environ["CASHLY_ENV"] == "staging"
    assert os.environ["CASHLY_ENV_SOURCE"] == "file"
