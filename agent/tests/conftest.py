"""Shared fixtures.

Deliberately minimal — each test builds the collaborators it needs and
passes them into `create_app` explicitly.  We don't want an implicit
"the app" fixture that quietly shares state across tests.
"""

from __future__ import annotations

import os

import pytest

# Ensure Settings uses the in-memory SQLite default before Settings caches.
os.environ.setdefault("COPILOT_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Wipe the `get_settings()` cache so per-test env changes take effect."""
    from copilot.config import get_settings

    get_settings.cache_clear()
