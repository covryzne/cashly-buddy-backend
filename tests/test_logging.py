import json
import logging
import sys

import pytest

from cashly.core.logging import JsonFormatter, TerminalFormatter, configure_logging


def record(status=200):
    item = logging.LogRecord("cashly.http", logging.INFO, "", 0, "request_completed", (), None)
    item.created = 0
    item.method = "GET"
    item.route = "/auth/configuration"
    item.status = status
    item.duration_ms = 1.52
    item.request_id = "test-request-id"
    return item


@pytest.mark.parametrize(("status", "color"), [(200, "32"), (302, "36"), (401, "33"), (503, "31")])
def test_terminal_status_color_and_safe_fields(status, color):
    output = TerminalFormatter().format(record(status))
    assert f"\033[{color}m{status}" in output
    assert "/auth/configuration" in output
    assert "1.52 ms" in output
    assert "07:00:00" in output  # Unix epoch converted to WIB.
    assert "rid=test-request-id" in output


def test_terminal_plain_text_and_control_character_sanitization():
    item = record()
    item.route = "/test\n\033[31m"
    output = TerminalFormatter(color=False).format(item)
    assert "\033" not in output
    assert "\n" not in output


def test_json_is_parseable_and_does_not_emit_arbitrary_extra_fields():
    item = record()
    item.password = "private-secret"
    output = JsonFormatter().format(item)
    assert json.loads(output)["status"] == 200
    assert "private-secret" not in output
    assert "\033" not in output


def test_auto_output_and_no_color(monkeypatch):
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    configure_logging("INFO")
    formatter = logging.getLogger("cashly").handlers[0].formatter
    assert isinstance(formatter, TerminalFormatter)
    assert "\033" not in formatter.format(record())
    monkeypatch.setattr(sys.stderr, "isatty", lambda: False)
    configure_logging("INFO")
    assert isinstance(logging.getLogger("cashly").handlers[0].formatter, JsonFormatter)
