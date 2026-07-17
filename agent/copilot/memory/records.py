"""Small return value-objects for the repository's chat + rounds methods.

These are *not* public API contracts (those live in `copilot.domain`) — they
are the typed shapes the `MemoryRepository` hands back so SQL rows never leak
to callers.  Frozen Pydantic models, same house style as the domain layer.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from copilot.domain.primitives import ClinicianId


class ConversationMessage(BaseModel):
    """One persisted turn in a conversation, as read back from storage."""

    model_config = ConfigDict(frozen=True)

    role: str
    content: str
    created_at: datetime


class RoundingCursor(BaseModel):
    """A clinician's persisted round position — hydrated from `rounding_cursor`."""

    model_config = ConfigDict(frozen=True)

    clinician_id: ClinicianId
    ordered_patient_ids: list[int]
    current_index: int
    completed_ids: list[int]
