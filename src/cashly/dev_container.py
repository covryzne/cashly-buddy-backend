"""Start the Compose dev backend with host-local Supabase settings."""

import os
import sys
from urllib.parse import urlsplit

from pydantic import SecretStr

from cashly.core.environment import ConfigurationError
from cashly.db.connection import database_url


def main() -> None:
    if os.environ.get("CASHLY_ENV") != "dev":
        raise SystemExit("The Compose dev launcher requires CASHLY_ENV=dev.")

    local_hosts = {"127.0.0.1", "localhost", "::1"}
    docker_host = "host.docker.internal"
    raw_database_url = os.environ.get("DATABASE_URL")
    if raw_database_url:
        try:
            url = database_url(SecretStr(raw_database_url), allow_docker_dev=True)
        except ConfigurationError as error:
            raise SystemExit(str(error)) from None
        if url.host in local_hosts and url.port == 54322 and url.database == "postgres":
            os.environ["DATABASE_URL"] = url.set(host=docker_host).render_as_string(
                hide_password=False
            )

    auth = urlsplit(os.environ.get("SUPABASE_URL", ""))
    if (
        auth.scheme == "http"
        and auth.hostname in local_hosts
        and auth.port == 54321
        and not (auth.username or auth.password or auth.query or auth.fragment)
        and auth.path in {"", "/"}
    ):
        os.environ["SUPABASE_URL"] = f"http://{docker_host}:54321"

    os.execv(sys.executable, [sys.executable, "-m", "cashly", "dev"])


if __name__ == "__main__":
    main()
