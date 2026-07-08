"""Observability contract + no-op implementation + correlation-id plumbing.

The Poller, Verifier, chat handler etc. all take an ``Observability``
via injection; nothing branches on "do we have Langfuse configured?".
When creds are absent, ``NoopObservability`` is a swap-in with the same
API.

Correlation IDs are stored in a `contextvars.ContextVar` so async tasks
inherit them via ``asyncio.copy_context()``.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, Protocol


correlation_id_var: ContextVar[str] = ContextVar("copilot_correlation_id", default="")


def generate_correlation_id() -> str:
    """New URL-safe correlation ID.  Short — 16 bytes of randomness."""
    return secrets.token_urlsafe(12)


def current_correlation_id() -> str:
    """The correlation ID for the running task, or '' if unset."""
    return correlation_id_var.get()


class Span(Protocol):
    """Opaque span handle — implementations own the details."""

    def set_attribute(self, key: str, value: Any) -> None: ...
    def set_output(self, value: Any) -> None: ...


class Observability(Protocol):
    """Protocol every observability backend implements."""

    @asynccontextmanager
    def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]: ...

    def event(self, name: str, **attributes: Any) -> None:
        """One-off event — no timing, no children."""

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        """A verification pass/fail event — matches the dashboard metric."""

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:
        """Poller staleness gauge — the top-failure-mode alert signal."""

    async def flush(self) -> None:
        """Ensure buffered events are sent (called at process exit)."""


# --- No-op ------------------------------------------------------------------


class _NoopSpan:
    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        return

    def set_output(self, value: Any) -> None:  # noqa: ARG002
        return


class NoopObservability:
    """Zero-cost placeholder — safe to inject when Langfuse is not wired."""

    @asynccontextmanager
    async def span(self, name: str, **attributes: Any) -> AsyncIterator[Span]:  # noqa: ARG002
        yield _NoopSpan()

    def event(self, name: str, **attributes: Any) -> None:  # noqa: ARG002
        return

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:  # noqa: ARG002
        return

    def record_poller_staleness(self, *, patient_id: int, age_seconds: int) -> None:  # noqa: ARG002
        return

    async def flush(self) -> None:
        return
