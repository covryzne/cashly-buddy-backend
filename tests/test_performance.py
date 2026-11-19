import logging
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from cashly.core.performance import instrument_engine, performance
from cashly.core.settings import Settings
from cashly.main import create_app


def test_request_query_metrics_are_isolated_across_worker_threads():
    engine = create_engine("sqlite://")
    instrument_engine(engine)
    app = create_app(Settings(environment="dev"))

    @app.get("/probe/{count}")
    def probe(count: int):
        with engine.connect() as connection:
            for _ in range(count):
                connection.execute(text("select :secret"), {"secret": "never-log-me"})
        return {"count": count}

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            if record.getMessage() == "request_completed":
                records.append(record)

    handler = Capture()
    logger = logging.getLogger("cashly.http")
    logger.addHandler(handler)
    try:
        with TestClient(app) as client:

            def call(count):
                rid = str(uuid4())
                response = client.get(f"/probe/{count}", headers={"X-Request-ID": rid})
                assert response.status_code == 200
                assert response.headers["X-Request-ID"] == rid
                assert "db;dur=" in response.headers["Server-Timing"]
                return rid, count

            with ThreadPoolExecutor(max_workers=2) as pool:
                expected = dict(pool.map(call, (1, 7)))
            assert {record.request_id: record.db_queries for record in records} == expected
            assert all(record.db_ms >= record.db_slowest_ms >= 0 for record in records)
            assert "never-log-me" not in str([vars(record) for record in records])
        assert performance.get() is None
    finally:
        logger.removeHandler(handler)
        engine.dispose()
