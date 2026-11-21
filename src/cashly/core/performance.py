"""Request-local numeric diagnostics. Never retain SQL, parameters or identity."""

from contextvars import ContextVar
from dataclasses import dataclass
from time import perf_counter

from sqlalchemy import event


@dataclass
class Performance:
    auth_ms: float = 0
    db_ms: float = 0
    db_queries: int = 0
    db_slowest_ms: float = 0
    db_connect_ms: float = 0

    def fields(self):
        return {
            name: round(value, 2) if isinstance(value, float) else value
            for name, value in vars(self).items()
        }


performance: ContextVar[Performance | None] = ContextVar("cashly_performance", default=None)


def instrument_engine(engine):
    def before(conn, cursor, statement, parameters, context, executemany):
        context.cashly_started = perf_counter()

    def finish(context):
        started = getattr(context, "cashly_started", None)
        metrics = performance.get()
        if metrics is not None and started is not None:
            duration = (perf_counter() - started) * 1000
            metrics.db_queries += 1
            metrics.db_ms += duration
            metrics.db_slowest_ms = max(metrics.db_slowest_ms, duration)
            context.cashly_started = None

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", lambda c, cu, s, p, ctx, many: finish(ctx))
    event.listen(engine, "handle_error", lambda error: finish(error.execution_context))
