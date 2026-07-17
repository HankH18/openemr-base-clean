"""Physician write-back orchestration (Phase 1b).

The write-side analogue of ``copilot.chat``: a thin service that turns a typed,
authorized write request into an append-only OpenEMR record through the
propose → confirm gate (see ``research/WRITEBACK_PHASE1_PLAN.md`` §3).

Nothing here is reachable while ``settings.writeback_enabled`` is false — the
routes return a clear "disabled" response and the write client refuses to build.
"""

from copilot.writeback.service import (
    IdempotencyStore,
    WriteInputError,
    WriteService,
    get_idempotency_store,
)

__all__ = [
    "IdempotencyStore",
    "WriteInputError",
    "WriteService",
    "get_idempotency_store",
]
