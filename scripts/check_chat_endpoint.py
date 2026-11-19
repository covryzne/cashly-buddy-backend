"""Live smoke test for the real chat-extraction endpoint.

Signs in to the configured Supabase project with the dev QA account, then POSTs
fictional messages to the running backend's /api/v1/chat/extract endpoint.

Never prints tokens, keys or raw provider bodies.

Usage (backend must already be running):
    .venv\\Scripts\\python.exe scripts\\check_chat_endpoint.py
    .venv\\Scripts\\python.exe scripts\\check_chat_endpoint.py --message "2 kopi total 30rb"
    .venv\\Scripts\\python.exe scripts\\check_chat_endpoint.py --app-env staging
"""

import argparse
import json
import sys
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cashly.core.environment import resolve_environment  # noqa: E402
from cashly.core.settings import Settings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MESSAGES = ["kopi 18rb", "2 kopi total 30rb", "makan siang 25rb kemarin"]


def load_account(email=None, password=None):
    if email and password:
        return email, password
    path = ROOT / ".cashly-dev-account.json"
    if not path.exists():
        raise SystemExit(
            "Missing .cashly-dev-account.json; pass --email and --password instead."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("email") or not data.get("password"):
        raise SystemExit(".cashly-dev-account.json must contain email and password.")
    return data["email"], data["password"]


def sign_in(settings, email, password):
    if settings.supabase_url is None or settings.supabase_publishable_key is None:
        raise SystemExit("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for sign-in.")
    with httpx2.Client(timeout=20, trust_env=False) as auth:
        response = auth.post(
            f"{settings.supabase_url}/auth/v1/token?grant_type=password",
            headers={"apikey": settings.supabase_publishable_key.get_secret_value()},
            json={"email": email, "password": password},
        )
    if response.status_code != 200:
        raise SystemExit(f"Sign-in failed with HTTP {response.status_code}.")
    token = response.json().get("access_token")
    if not token:
        raise SystemExit("Sign-in returned no access token.")
    return token


def summarize(payload):
    data = payload.get("data") or {}
    entries = data.get("entries") or []
    print(f"  reply: {data.get('reply')!r}")
    for entry in entries:
        draft = entry.get("draft")
        if not draft:
            print(f"  entry: pending issue={entry.get('issue')!r}")
            continue
        items = [(i["name"], i["quantity"], i["priceIdr"]) for i in draft["items"]]
        print(
            f"  entry: cat={draft['categoryCode']} date={draft['occurredDate']} "
            f"time={draft['occurredTime']} items={items}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-env", choices=["dev", "staging", "prod"], default="dev")
    parser.add_argument("--base-url", default=None, help="Default: http://HOST:PORT from env")
    parser.add_argument("--message", action="append", help="Repeatable; defaults to 3 samples")
    parser.add_argument("--email", default=None, help="Override the QA account email")
    parser.add_argument("--password", default=None, help="Override the QA account password")
    args = parser.parse_args()

    settings = Settings.from_selection(resolve_environment(root=ROOT, override=args.app_env))
    base_url = args.base_url or f"http://{settings.host}:{settings.port}"
    messages = args.message or DEFAULT_MESSAGES

    print(
        json.dumps(
            {
                "base_url": base_url,
                "environment": settings.environment,
                # The server's provider/model come from its own env; this is only the
                # local file's view and may differ from the running process.
                "local_env_provider": settings.effective_ai_provider,
                "local_env_model": settings.openrouter_model
                if settings.effective_ai_provider == "openrouter"
                else settings.gemini_model,
            }
        )
    )

    email, password = load_account(args.email, args.password)
    token = sign_in(settings, email, password)
    headers = {"Authorization": f"Bearer {token}"}

    failures = 0
    with httpx2.Client(timeout=90, trust_env=False) as api:
        health = api.get(f"{base_url}/api/v1/health")
        print(f"health: {health.status_code}")
        for message in messages:
            print(f"\nmessage: {message!r}")
            try:
                response = api.post(
                    f"{base_url}/api/v1/chat/extract", headers=headers, json={"message": message}
                )
            except httpx2.HTTPError as exc:
                print(f"  FAIL transport {type(exc).__name__}")
                failures += 1
                continue
            if response.status_code != 200:
                error = response.json().get("error", {})
                print(f"  FAIL {response.status_code} {error.get('code')}: {error.get('message')}")
                print(f"  request_id: {error.get('request_id')}")
                failures += 1
                continue
            summarize(response.json())
    print(f"\n{len(messages) - failures}/{len(messages)} succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
