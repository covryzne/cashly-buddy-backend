"""Opt-in live providers with real local API/PG, synthetic browser identity, and cleanup."""

import json
import logging
import os
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import uvicorn
from sqlalchemy import delete, func, select, text

from cashly.core.auth import Identity, require_identity
from cashly.core.environment import resolve_environment
from cashly.core.settings import Settings
from cashly.db.commands import upgrade_database
from cashly.db.connection import Database, database_url
from cashly.db.models import Account, Base, Expense, ExpenseItem
from cashly.main import create_app
from cashly.services.accounts import bootstrap


@pytest.mark.skipif(os.getenv("CASHLY_LIVE_AI") != "1", reason="Opt-in live AI verification")
def test_live_multimodal_browser_with_disposable_database(postgres_settings, tmp_path):
    backend = Path(__file__).resolve().parents[1]
    frontend = backend.parent / "cashly-buddy-frontend"
    report_path = backend.parent / "specs/verification/ai-live-e2e.json"
    stage = Settings.from_selection(resolve_environment(root=backend, override="staging"))
    # Only copy AI configuration. Never copy a hosted database or its auth settings.
    config = {
        name: getattr(stage, name)
        for name in (
            "ai_provider",
            "ai_audio_provider",
            "gemini_api_key",
            "gemini_model",
            "gemini_audio_model",
            "gemini_image_model",
            "gemini_timeout_seconds",
            "gemini_thinking_level",
            "openrouter_api_key",
            "openrouter_model",
            "openrouter_audio_model",
            "openrouter_image_model",
            "openrouter_timeout_seconds",
        )
    }
    settings = postgres_settings.model_copy(update=config)
    assert database_url(settings.database_url).host == "127.0.0.1"
    assert settings.environment == "dev"
    upgrade_database(settings)
    db = Database(settings)
    identity = Identity(
        UUID("10000000-0000-4000-8000-000000000001"),
        "ai-live@example.invalid",
        "AI Live Tester",
        datetime.now(UTC),
    )
    with db.transaction() as session:
        session.execute(text("INSERT INTO auth.users(id) VALUES (:id)"), {"id": identity.user_id})
    account = bootstrap(db, settings, identity)
    app = create_app(settings)
    app.dependency_overrides[require_identity] = lambda: identity
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=34569, access_log=False, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    report = {
        "provider": settings.effective_ai_provider,
        "database": "disposable_local_postgresql",
        "auth": "synthetic_loopback_identity",
        "live_ai": True,
        "hosted_database_writes": 0,
        "browser_passed": False,
        "cleanup_passed": False,
    }
    measured = []

    class Metrics(logging.Handler):
        def emit(self, record):
            if record.getMessage() not in {
                "ai_extraction_completed",
                "request_completed",
                "ai_extraction_audio_validated",
            }:
                return
            allowed = (
                "provider",
                "model",
                "resolved_model",
                "input_kind",
                "phase",
                "outcome",
                "duration_ms",
                "audio_duration_seconds",
                "decode_ms",
                "route",
                "method",
                "status",
                "db_ms",
                "db_queries",
                "upstream_status",
            )
            measured.append(
                {
                    "event": record.getMessage(),
                    **{
                        field: getattr(record, field) for field in allowed if hasattr(record, field)
                    },
                }
            )

    metrics = Metrics()
    logging.getLogger("cashly").addHandler(metrics)
    started = time.perf_counter()
    try:
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        env = {
            **os.environ,
            "CASHLY_TEST_REAL_API": "http://127.0.0.1:34569",
            "CASHLY_TEST_BUILD": "offline",
            "CASHLY_LIVE_AUDIO_PATH": str(backend / ".pytest-tmp-vn-live-assets/expense.wav"),
        }
        options = {
            "cwd": frontend,
            "env": env,
            "timeout": 240,
            "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        }
        with (tmp_path / "browser.log").open("w", encoding="utf-8") as output:
            result = subprocess.run(
                [
                    "node",
                    "node_modules/@playwright/test/cli.js",
                    "test",
                    "--config=playwright.ai-live.config.ts",
                ],
                stdout=output,
                stderr=subprocess.STDOUT,
                **options,
            )
        report["browser_passed"] = result.returncode == 0
        with db.transaction() as session:
            rows = list(
                session.scalars(select(Expense).where(Expense.account_id == account.account_id))
            )
            report["stored_expenses"] = len(rows)
            report["stored_sources"] = sorted(row.source_type for row in rows)
            report["soft_deleted_by_browser"] = sum(row.deleted_at is not None for row in rows)
            report["saved_totals_match"] = sorted(row.amount_idr for row in rows) == [12000, 15000]
        assert result.returncode == 0, "Live browser failed; inspect the disposable browser.log"
        assert report["stored_expenses"] == 2 and report["saved_totals_match"]
        assert report["soft_deleted_by_browser"] == 2
    finally:
        server.should_exit = True
        if thread.is_alive():
            thread.join(timeout=10)
        # Hard-delete only this disposable account's records, including reward events.
        with db.transaction() as session:
            expense_ids = select(Expense.id).where(Expense.account_id == account.account_id)
            session.execute(delete(ExpenseItem).where(ExpenseItem.expense_id.in_(expense_ids)))
            for table in reversed(Base.metadata.sorted_tables):
                if table.schema == "cashly" and "account_id" in table.c:
                    session.execute(table.delete().where(table.c.account_id == account.account_id))
            session.execute(delete(Account).where(Account.id == account.account_id))
            session.execute(text("DELETE FROM auth.users WHERE id=:id"), {"id": identity.user_id})
        with db.transaction() as session:
            report["cleanup_passed"] = (
                session.scalar(select(func.count()).select_from(Expense)) == 0
                and session.get(Account, account.account_id) is None
            )
        report["measurements"] = measured
        logging.getLogger("cashly").removeHandler(metrics)
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        db.dispose()
    assert report["cleanup_passed"]
