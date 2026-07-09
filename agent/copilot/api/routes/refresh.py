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

from fastapi import APIRouter
from pydantic import BaseModel, Field

from copilot.config import get_settings
from copilot.domain.primitives import ClinicianId
from copilot.worker.pipeline import RefreshPipeline

router = APIRouter(prefix="/v1/rounds", tags=["rounds"])


class RefreshRequest(BaseModel):
    """Force a re-sync of one clinician's active rounding list."""

    clinician_id: int = Field(gt=0)


@router.post("/refresh", summary="Re-sync the clinician's rounding list; report per patient")
async def refresh(req: RefreshRequest) -> dict[str, Any]:
    pipeline = RefreshPipeline(get_settings())
    results = await pipeline.refresh(ClinicianId(value=req.clinician_id))
    return {"results": [r.model_dump(mode="json") for r in results]}
