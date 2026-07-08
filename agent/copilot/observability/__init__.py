"""Observability — Langfuse + correlation IDs.

ARCHITECTURE §"Observability": one correlation ID per request/tick,
threaded through every LLM call, tool call, verification step, and log
line.  Alerts on p95 latency, error rate, tool-failure rate, and poller
staleness.

Two implementations behind one Protocol:

- ``LangfuseObservability`` — real SDK; refuses to construct without
  ``LANGFUSE_HOST``/``PUBLIC_KEY``/``SECRET_KEY``.
- ``NoopObservability`` — always available; used in tests and whenever
  the operator hasn't wired Langfuse yet, so nothing else has to
  branch on "is observability configured".
"""

from copilot.observability.base import (
    NoopObservability,
    Observability,
    Span,
    correlation_id_var,
    current_correlation_id,
    generate_correlation_id,
)
from copilot.observability.factory import build_observability

__all__ = [
    "NoopObservability",
    "Observability",
    "Span",
    "build_observability",
    "correlation_id_var",
    "current_correlation_id",
    "generate_correlation_id",
]
