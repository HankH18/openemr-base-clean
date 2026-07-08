"""Persistence for the agent-owned Postgres.

Everything that touches the DB goes through the repository interface —
never raw SQL from application code — so the store is swappable and the
same code runs against Postgres in prod and SQLite (aiosqlite) in tests.
"""

from copilot.memory.db import Base, get_engine, get_session_factory, session_scope
from copilot.memory.models import (
    AuditLogRow,
    ConversationRow,
    LastSeenRow,
    MemoryFileRow,
    MessageRow,
    RoundingCursorRow,
    SyncStateRow,
)
from copilot.memory.repository import MemoryRepository

__all__ = [
    "Base",
    "get_engine",
    "get_session_factory",
    "session_scope",
    "AuditLogRow",
    "ConversationRow",
    "LastSeenRow",
    "MemoryFileRow",
    "MemoryRepository",
    "MessageRow",
    "RoundingCursorRow",
    "SyncStateRow",
]
