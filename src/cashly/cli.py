import argparse
import json
import os

import uvicorn

from cashly.core.environment import ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.commands import check_database, render_migration_sql, upgrade_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Cashly API environment-aware launcher")
    parser.add_argument(
        "command", choices=["dev", "start", "check", "db-check", "db-upgrade", "db-sql"]
    )
    parser.add_argument("--app-env", choices=["dev", "staging", "prod"])
    parser.add_argument("--env-source", choices=["file", "system"])
    args = parser.parse_args()
    if args.command == "db-sql":
        # Reviewed SQL preview requires no dotenv or live credentials.
        print(render_migration_sql())
        return
    try:
        selected = resolve_environment(override=args.app_env, source_override=args.env_source)
        settings = Settings.from_selection(selected)
        if args.command == "dev" and selected.environment != "dev":
            raise ConfigurationError("Use start for staging/prod; reload is development-only.")
        if args.command == "db-check":
            print(json.dumps(check_database(settings)))
            return
        if args.command == "db-upgrade":
            upgrade_database(settings)
            print("[Cashly] development migration applied successfully.")
            return
    except ConfigurationError as exc:
        parser.exit(2, f"[Cashly] {exc}\n")
    print(
        f"[Cashly] environment={selected.environment} source={selected.source} "
        f"file={selected.filename if selected.source == 'file' else 'none'}",
        flush=True,
    )
    if args.command == "check":
        return
    # Reload children inherit explicit selection; no branch drift during restart.
    os.environ["CASHLY_ENV"] = selected.environment
    os.environ["CASHLY_ENV_SOURCE"] = selected.source
    uvicorn.run(
        "cashly.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=args.command == "dev",
        log_level=settings.log_level.lower(),
        access_log=False,
        proxy_headers=False,
    )
