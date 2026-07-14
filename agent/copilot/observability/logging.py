"""Structured JSON logging — one line of JSON per record, correlation-tagged.

Wires ``logging.config.dictConfig`` so every log record is emitted as a single
line of JSON on stdout (what a container log collector scrapes) and carries the
request's ``correlation_id`` (the same id echoed on the ``X-Correlation-ID``
response header). The id is injected by :class:`CorrelationIdFilter`, which
reads the request-scoped ``contextvars`` value, so handlers never have to thread
it through call signatures.

PHI discipline: this configures the *transport* only. Call sites decide what to
log, and must never place raw patient identifiers, document text, or extracted
clinical values into a record — PSR-3-style structured context with stable,
non-PHI keys (method/path/status/latency/ids-as-opaque-integers) only. The
frozen ``phi_check`` scans the captured corpus these records feed.
"""

from __future__ import annotations

import logging
import logging.config
import os
from typing import Any

from copilot.observability.base import current_correlation_id

_STDOUT_FD = 1


class StdoutFdHandler(logging.Handler):
    """Emit each formatted record to the process stdout file descriptor (fd 1).

    Targets the OS-level stdout fd directly rather than the ``sys.stdout`` Python
    object. In a container fd 1 is the log stream a collector scrapes; targeting
    the fd (not a cached Python stream object) keeps a single line per record and
    stays robust to Python-level stream swapping (test capture layers), so
    fd-level log capture always sees the record.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            os.write(_STDOUT_FD, line.encode("utf-8", errors="replace"))
        except Exception:
            # A logging handler must never raise into the caller; defer to the
            # framework's error hook.
            self.handleError(record)


class CorrelationIdFilter(logging.Filter):
    """Attach the request-scoped correlation id to every record.

    Reads the ``contextvars`` value the correlation-id middleware published for
    the running task; records emitted outside any request carry ``""`` (an empty
    string), which downstream tooling treats as "unset" rather than a real id.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id()
        return True


def logging_config(level: str = "INFO") -> dict[str, Any]:
    """The ``dictConfig`` document — JSON to stdout, correlation-id on every record.

    Split out from :func:`configure_logging` so it is unit-inspectable and so the
    exact same document backs both the app and any script that wants structured
    logs. ``disable_existing_loggers`` is ``False`` so module loggers created at
    import time keep working after (re)configuration.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "correlation_id": {
                "()": "copilot.observability.logging.CorrelationIdFilter",
            },
        },
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                # Named fields become top-level JSON keys; ``correlation_id`` is
                # supplied by the filter above, present on every line.
                "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s %(correlation_id)s",
            },
        },
        "handlers": {
            "stdout": {
                "()": "copilot.observability.logging.StdoutFdHandler",
                "formatter": "json",
                "filters": ["correlation_id"],
            },
        },
        "root": {
            "level": level,
            "handlers": ["stdout"],
        },
    }


def configure_logging(level: str = "INFO") -> None:
    """Activate structured JSON logging process-wide (idempotent).

    Safe to call repeatedly — ``dictConfig`` replaces the root configuration
    each time rather than stacking handlers, so building the app more than once
    (tests, workers) never duplicates log lines.
    """
    logging.config.dictConfig(logging_config(level))
