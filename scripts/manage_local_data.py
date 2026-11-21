"""Inspect/reset local transactions or seed reference categories; no Auth deletion."""

import argparse
import json

from cashly.core.environment import ConfigurationError, resolve_environment
from cashly.core.settings import Settings
from cashly.db.local_data import local_data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "reset", "seed-reference"])
    parser.add_argument("--env-source", choices=["file", "system"], default="file")
    parser.add_argument("--confirm", help="For reset only: cashly-local-transactions")
    args = parser.parse_args()
    try:
        selection = resolve_environment(override="dev", source_override=args.env_source)
        settings = Settings.from_selection(selection)
        print(json.dumps(local_data(settings, args.action, confirm=args.confirm)))
    except ConfigurationError as error:
        parser.exit(2, f"[Cashly] {error}\n")


if __name__ == "__main__":
    main()
