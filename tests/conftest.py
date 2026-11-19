"""Disposable local PostgreSQL only. Never inspect developer env/remote databases."""

import os
import shutil
import socket
import subprocess
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr

from cashly.core.settings import Settings


@pytest.fixture(scope="session")
def postgres_settings(tmp_path_factory):
    executable = shutil.which("pg_ctl")
    if executable is None:
        pytest.skip("Local PostgreSQL binaries are required for integration tests.")
    bindir = Path(executable).parent
    root = tmp_path_factory.mktemp("postgres")
    # tmp_path_factory resolves inside backend .pytest-tmp per project config.
    assert root.resolve().is_relative_to(Path(__file__).resolve().parents[1])
    data = root / "data"
    log = root / "server.log"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if key in os.environ
    }
    options = {
        "capture_output": True,
        "text": True,
        "env": env,
        "timeout": 30,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    result = subprocess.run(
        [
            str(bindir / "initdb"),
            "-D",
            str(data),
            "--auth=trust",
            "--username=cashly_test_admin",
            "--locale=C",
            "--encoding=UTF8",
        ],
        check=False,
        **options,
    )
    if result.returncode != 0:
        pytest.fail(f"Local test initdb failed: {result.stderr}", pytrace=False)
    started = False
    # pg_ctl starts a background server. Captured Windows pipe handles can stay
    # open in the child; use files/DEVNULL so communicate() cannot wait forever.
    server_options = {key: value for key, value in options.items() if key != "capture_output"}
    server_options.update(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.run(
            [
                executable,
                "-D",
                str(data),
                "-l",
                str(log),
                "-w",
                "-t",
                "15",
                "-o",
                f"-h 127.0.0.1 -p {port} -F -c ssl=off",
                "start",
            ],
            check=False,
            **server_options,
        )
        if result.returncode != 0:
            pytest.fail(
                "Local test pg_ctl failed; inspect the disposable server log.", pytrace=False
            )
        started = True
        url = f"postgresql+psycopg://cashly_test_admin@127.0.0.1:{port}/postgres?sslmode=disable"
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="cashly_test_admin",
            dbname="postgres",
            sslmode="disable",
        ) as conn:
            conn.execute("CREATE ROLE anon NOLOGIN")
            conn.execute("CREATE ROLE authenticated NOLOGIN")
            conn.execute("CREATE SCHEMA auth")
            conn.execute("CREATE TABLE auth.users (id uuid PRIMARY KEY)")
            conn.execute("CREATE TABLE public.unrelated_sentinel (id integer PRIMARY KEY)")
        yield Settings(
            environment="dev",
            database_url=SecretStr(url),
            migration_database_url=SecretStr(url),
            db_pool_size=1,
        )
    finally:
        if started:
            subprocess.run(
                [executable, "-D", str(data), "-m", "fast", "-w", "stop"],
                check=True,
                **server_options,
            )
