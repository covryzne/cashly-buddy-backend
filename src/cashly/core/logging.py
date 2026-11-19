import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        for key in (
            "request_id",
            "method",
            "route",
            "status",
            "duration_ms",
            "auth_ms",
            "db_ms",
            "db_queries",
            "db_slowest_ms",
            "db_connect_ms",
            "exception_type",
            "provider",
            "model",
            "model_version",
            "thinking_level",
            "input_kind",
            "upstream_status",
            "provider_error_status",
            "provider_error_reason",
            "transport",
            "api_version",
            "resolved_model",
            "upstream_provider",
            "provider_error_code",
            "requested_provider",
            "fallback_reason",
            "endpoint",
            "attempt",
            "outcome",
        ):
            if hasattr(record, key):
                entry[key] = getattr(record, key)
        return json.dumps(entry, ensure_ascii=True)


class TerminalFormatter(logging.Formatter):
    """Readable terminal output using only the existing safe log fields."""

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def paint(self, value, code: str) -> str:
        value = re.sub(r"[\x00-\x1f\x7f-\x9f]", "?", str(value))
        return f"\033[{code}m{value}\033[0m" if self.color else value

    def format(self, record: logging.LogRecord) -> str:
        clock = datetime.fromtimestamp(record.created, ZoneInfo("Asia/Jakarta")).strftime(
            "%H:%M:%S"
        )
        severity = (
            "31"
            if record.levelno >= logging.ERROR
            else "33"
            if record.levelno >= logging.WARNING
            else "36"
        )
        prefix = f"{self.paint(clock, '90')} {self.paint(record.levelname.ljust(7), severity)}"
        if record.getMessage() == "request_completed":
            status = getattr(record, "status", 500)
            code = (
                "31"
                if status >= 500
                else "33"
                if status >= 400
                else "36"
                if status >= 300
                else "32"
            )
            method = self.paint(getattr(record, "method", "HTTP").ljust(6), "35")
            route = self.paint(getattr(record, "route", "unmatched"), "37")
            result = self.paint(status, code)
            duration = self.paint(f"{getattr(record, 'duration_ms', 0):.2f} ms", "90")
            request_id = self.paint(f"rid={getattr(record, 'request_id', '-')}", "90")
            insight = " ".join(
                f"{key}={getattr(record, key):.2f}"
                for key in ("auth_ms", "db_ms", "db_connect_ms", "db_slowest_ms")
                if hasattr(record, key)
            )
            queries = f" queries={record.db_queries}" if hasattr(record, "db_queries") else ""
            return (
                f"{prefix} {method} {route}  {result}  {duration}  {insight}{queries}  {request_id}"
            )
        event = self.paint(record.getMessage(), severity)
        if record.getMessage().startswith(("ai_extraction_", "ai_provider_")):
            fields = " ".join(
                f"{key}={self.paint(getattr(record, key), '90')}"
                for key in (
                    "provider",
                    "model",
                    "model_version",
                    "thinking_level",
                    "input_kind",
                    "upstream_status",
                    "provider_error_status",
                    "provider_error_reason",
                    "transport",
                    "api_version",
                    "resolved_model",
                    "upstream_provider",
                    "provider_error_code",
                    "requested_provider",
                    "fallback_reason",
                    "endpoint",
                    "exception_type",
                    "attempt",
                    "outcome",
                    "duration_ms",
                )
                if hasattr(record, key)
            )
            return f"{prefix} {event} {fields} rid={getattr(record, 'request_id', '-')}"
        kind = self.paint(getattr(record, "exception_type", ""), "31")
        request_id = self.paint(f"rid={getattr(record, 'request_id', '-')}", "90")
        return f"{prefix} {event} {kind} {request_id}".rstrip()


def configure_logging(level: str, log_format: str = "auto", log_color: str = "auto") -> None:
    logger = logging.getLogger("cashly")
    logger.setLevel(level)
    handler = logging.StreamHandler()
    terminal = sys.stderr.isatty()
    pretty = log_format == "pretty" or (log_format == "auto" and terminal)
    color = log_color == "always" or (
        log_color == "auto" and terminal and "NO_COLOR" not in os.environ
    )
    handler.setFormatter(TerminalFormatter(color) if pretty else JsonFormatter())
    logger.handlers = [handler]
    logger.propagate = False
