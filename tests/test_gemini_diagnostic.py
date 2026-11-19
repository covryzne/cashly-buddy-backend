"""Basic diagnostic uses a single real loopback HTTP request and redacts failures."""

import asyncio
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import SecretStr

from cashly.core.settings import Settings


@pytest.mark.parametrize("api_version", ["v1", "v1beta"])
@pytest.mark.parametrize("status", [200, 503])
@pytest.mark.parametrize("sdk", [False, True])
def test_basic_rest_is_independent_of_extraction_schema_and_hides_private_errors(
    monkeypatch,
    capsys,
    api_version,
    status,
    sdk,
):
    spec = importlib.util.spec_from_file_location(
        "gemini_diagnostic",
        Path(__file__).resolve().parents[1] / "scripts/check_gemini.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(
                {
                    "path": self.path,
                    "key": self.headers.get("x-goog-api-key"),
                    "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                }
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = (
                {
                    "id": "int-synthetic",
                    "object": "interaction",
                    "status": "completed",
                    "model": "gemini-3.8-flash",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {"type": "text", "text": "OK"},
                            ],
                        }
                    ],
                }
                if status == 200
                else {
                    "error": {
                        "status": "UNAVAILABLE",
                        "message": "overloaded private-upstream-text",
                    },
                }
            )
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(module, "GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1beta")
    try:
        settings = Settings(
            environment="staging",
            gemini_api_key=SecretStr("private-test-key"),
            gemini_thinking_level="medium",
        )
        result = asyncio.run(
            module.check(settings, False, basic=not sdk, sdk_basic=sdk, api_version=api_version)
        )
        output = capsys.readouterr().out
        assert result == (0 if status == 200 else 1)
        assert "private-test-key" not in output
        assert "private-upstream-text" not in output
        rows = [json.loads(line) for line in output.splitlines()]
        assert rows[0]["thinking_level"] == "model_default"
        assert rows[1]["status"] == status
        if status == 503:
            assert rows[1]["provider_error_status"] == "UNAVAILABLE"
            assert rows[1]["provider_error_reason"] == "capacity"
        assert len(calls) == 1
        assert calls[0]["path"] == f"/{api_version}/interactions"
        assert calls[0]["key"] == "private-test-key"
        assert calls[0]["body"]["model"] == "gemini-3.8-flash"
        assert calls[0]["body"]["input"] == "Reply only OK."
        assert calls[0]["body"]["store"] is False
        assert "response_format" not in calls[0]["body"]
        assert "generation_config" not in calls[0]["body"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
