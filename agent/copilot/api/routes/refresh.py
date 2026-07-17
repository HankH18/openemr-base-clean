"""Manual rounding-list refresh endpoint.

``POST /v1/rounds/refresh`` forces a change-gated re-sync of every patient in
the clinician's rounding list: poll → verify-at-synthesis → score → persist.
It is the acceptance path for the background update loop (the auto-scheduler
is out of scope here); a clinician who suspects a chart has moved can pull the
freshest grounded summary on demand.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from copilot.api.deps import resolve_acting_clinician
from copilot.config import get_settings
from copilot.worker.pipeline import RefreshPipeline

router = APIRouter(prefix="/v1/rounds", tags=["rounds"])


class RefreshRequest(BaseModel):
    """Force a re-sync of one clinician's active rounding list.

    ``clinician_id`` is optional: in ``disabled`` mode it identifies the acting
    clinician (as today); in ``smart`` mode the session cookie is authoritative
    and this field, if present, is only validated against it (mismatch → 403).
    """

    clinician_id: int | None = Field(default=None, gt=0)


@router.post("/refresh", summary="Re-sync the clinician's rounding list; report per patient")
async def refresh(req: RefreshRequest, request: Request) -> dict[str, Any]:
    # Identity per the auth-mode contract: disabled → the body clinician_id;
    # smart → the session cookie (401 if none, 403 if the body id disagrees).
    cid = await resolve_acting_clinician(get_settings(), request, req.clinician_id)
    pipeline = RefreshPipeline(get_settings())
    results = await pipeline.refresh(cid)
    return {"results": [r.model_dump(mode="json") for r in results]}
