"""Real Langfuse backend.

Requires ``LANGFUSE_HOST``, ``LANGFUSE_PUBLIC_KEY``, and
``LANGFUSE_SECRET_KEY``.  Wraps the SDK's trace/span API and threads
the current correlation ID into every span so cross-service traces
line up.

Spans NEST: the first span opened under a correlation id becomes the trace
root (``client.trace(id=correlation_id)``); any span opened while another is
still open is created as a CHILD of the enclosing observation
(``parent.span(...)``), and one-off events attach to the enclosing observation
too. Every observation therefore carries the correlation id as its trace id, so
a whole multi-agent trace reconstructs from the correlation id alone. The
enclosing observation is tracked in a :class:`~contextvars.ContextVar`, so the
nesting is task-local and safe under concurrent requests.

The SDK import is lazy — this module never fails to import when the
package is absent; it only fails at construction if the operator
tried to build a Langfuse backend without the SDK installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from typing import Any

from copilot.observability.base import Observability, Span, current_correlation_id

# The Langfuse observation (trace root or span) currently open on this task, so
# a span/event opened within it can be created as its child. ``None`` ⇒ open a
# new trace root keyed by the correlation id.
_current_observation: ContextVar[Any | None] = ContextVar(
    "copilot_langfuse_observation", default=None
)


class _LangfuseSpan:
    """Adapter for one Langfuse span/observation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def set_attribute(self, key: str, value: Any) -> None:
        with suppress(Exception):
            self._inner.update(metadata={key: value})

    def set_output(self, value: Any) -> None:
        with suppress(Exception):
            self._inner.update(output=value)


class LangfuseObservability(Observability):
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        client: Any | None = None,
    ) -> None:
        if not (host and public_key and secret_key):
            raise RuntimeError("LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY all required to run Langfuse.")
        if client is not None:
            self._client = client
        else:
            from langfuse import Langfuse  # local import — keeps unit tests dep-light

            self._client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]:
        cid = current_correlation_id()
        parent = _current_observation.get()
        try:
            if parent is not None:
                # Nested: a child of the enclosing observation (inherits its
                # trace id). Keeps the whole multi-agent trace under one id.
                inner = parent.span(name=name, metadata=attributes)
            else:
                # Trace root for this correlation id.
                inner = self._client.trace(name=name, id=cid or None, metadata=attributes)
        except Exception:
            from copilot.observability.base import _NoopSpan

            yield _NoopSpan()
            return
        span = _LangfuseSpan(inner)
        token = _current_observation.set(inner)
        try:
            yield span
        finally:
            _current_observation.reset(token)
            with suppress(Exception):
                inner.end()

    def event(self, name: str, **attributes: Any) -> None:
        parent = _current_observation.get()
        with suppress(Exception):
            if parent is not None:
                # Attach the event to the enclosing observation so it lands
                # inside the current trace rather than as an orphan.
                parent.event(name=name, metadata=attributes)
            else:
                self._client.event(
                    name=name, trace_id=current_correlation_id() or None, metadata=attributes
                )

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.event(
            "verification.result",
            passed=passed,
            action=action,
            patient_id=patient_id,
            correlation_id=current_correlation_id(),
        )

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        self.event(
            "poller.staleness",
            patient_id=patient_id,
            age_seconds=age_seconds,
            correlation_id=current_correlation_id(),
        )

    async def flush(self) -> None:
        with suppress(Exception):
            self._client.flush()
