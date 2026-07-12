"""Proactive deterioration alerts (UC-5).

``GET /v1/rounds/alerts?clinician_id=int`` returns the not-yet-seen patients
on the clinician's list whose persisted acuity has crossed the alert
threshold — the critical patient who would otherwise sit unseen at the bottom
of the round. The rule is deliberately deterministic (persisted acuity vs a
configured threshold), so an alert always traces back to a grounded finding.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request

from copilot.api.deps import resolve_acting_clinician
from copilot.config import get_settings
from copilot.worker.pipeline import RefreshPipeline

router = APIRouter(prefix="/v1/rounds", tags=["rounds"])


@router.get("/alerts", summary="Deterioration alerts for not-yet-seen critical patients")
async def alerts(
    request: Request, clinician_id: Annotated[int | None, Query(gt=0)] = None
) -> dict[str, Any]:
    # Identity per the auth-mode contract: disabled → the query clinician_id;
    # smart → the session cookie (401 if none, 403 if the query id disagrees).
    cid = await resolve_acting_clinician(get_settings(), request, clinician_id)
    pipeline = RefreshPipeline(get_settings())
    found = await pipeline.alerts(cid)
    return {"alerts": [a.model_dump(mode="json") for a in found]}
