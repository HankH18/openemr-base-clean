"""Feature route modules for the Clinical Co-Pilot API.

Modules dropped into this package are auto-discovered at app startup by
``copilot.api.app.register_routers``: any module exposing a module-level
``router`` (a FastAPI ``APIRouter``) is included automatically, so adding a
feature route never requires editing ``app.py``.
"""
