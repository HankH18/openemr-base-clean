"""Real Langfuse backend.

Requires ``LANGFUSE_HOST``, ``LANGFUSE_PUBLIC_KEY``, and
``LANGFUSE_SECRET_KEY``.  Wraps the SDK's trace/span API and threads
the current correlation ID into every span so cross-service traces
line up.

The SDK import is lazy — this module never fails to import when the
package is absent; it only fails at construction if the operator
tried to build a Langfuse backend without the SDK installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from copilot.observability.base import Observability, Span, current_correlation_id


class _LangfuseSpan:
    """Adapter for one Langfuse span/observation."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def set_attribute(self, key: str, value: Any) -> None:
        try:
            self._inner.update(metadata={key: value})
        except Exception:  # noqa: BLE001 — telemetry never breaks callers
            pass

    def set_output(self, value: Any) -> None:
        try:
            self._inner.update(output=value)
        except Exception:  # noqa: BLE001
            pass


class LangfuseObservability(Observability):
    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        client: Any | None = None,
    ) -> None:
        if not (host and public_key and secret_key):
            raise RuntimeError(
                "LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY all required to run Langfuse."
            )
        if client is not None:
            self._client = client
        else:
            from langfuse import Langfuse  # local import — keeps unit tests dep-light

            self._client = Langfuse(host=host, public_key=public_key, secret_key=secret_key)

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]:
        cid = current_correlation_id()
        try:
            inner = self._client.trace(name=name, id=cid or None, metadata=attributes)
        except Exception:  # noqa: BLE001 — never fail callers on telemetry
            from copilot.observability.base import _NoopSpan

            yield _NoopSpan()
            return
        span = _LangfuseSpan(inner)
        try:
            yield span
        finally:
            try:
                inner.end()
            except Exception:  # noqa: BLE001
                pass

    def event(self, name: str, **attributes: Any) -> None:
        try:
            self._client.event(name=name, metadata=attributes)
        except Exception:  # noqa: BLE001
            pass

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
        try:
            self._client.flush()
        except Exception:  # noqa: BLE001
            pass
