"""Choose the right ``Observability`` given the current settings.

If all three Langfuse env vars are set, returns a real
``LangfuseObservability``.  Otherwise a ``NoopObservability`` — no
warnings, no branching required in callers.
"""

from __future__ import annotations

from copilot.config import Settings
from copilot.observability.base import NoopObservability, Observability


def build_observability(settings: Settings) -> Observability:
    if not (
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    ):
        return NoopObservability()
    from copilot.observability.langfuse_backend import LangfuseObservability

    return LangfuseObservability(
        host=settings.langfuse_host,
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
    )
