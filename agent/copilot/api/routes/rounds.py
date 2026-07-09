"""Rounding-session API — start, current, advance.

A clinician rounds one patient at a time, sickest first. ``start`` establishes
the authorized list plus a persisted cursor and returns the top card;
``current`` re-reads the cursor; ``advance`` marks the current patient seen and
moves to the next. The cursor is persisted, so a process restart resumes
mid-round.

Mounted automatically by ``copilot.api.app.register_routers`` (it exposes a
module-level ``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from copilot.config import get_settings
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.rounds.service import NoActiveRoundError, RoundsService, RoundView

router = APIRouter(prefix="/v1/rounds", tags=["rounds"])


class StartRequest(BaseModel):
    """Begin a round for one clinician over an authorized patient list."""

    clinician_id: int = Field(gt=0)
    patient_ids: list[int] = Field(min_length=1)


class AdvanceRequest(BaseModel):
    """Mark the current patient done and move to the next."""

    clinician_id: int = Field(gt=0)
    completed_patient_id: int = Field(gt=0)


def _service() -> RoundsService:
    return RoundsService(get_settings())


def _view_body(view: RoundView) -> dict[str, Any]:
    return {"current": view.current, "order": view.order}


@router.post("/start", summary="Begin a round; returns the sickest patient's card")
async def start(req: StartRequest) -> dict[str, Any]:
    view = await _service().start(
        ClinicianId(value=req.clinician_id),
        [PatientId(value=pid) for pid in req.patient_ids],
    )
    return _view_body(view)


@router.get("/current", summary="The current patient card for this clinician")
async def current(clinician_id: Annotated[int, Query(gt=0)]) -> dict[str, Any]:
    try:
        view = await _service().current(ClinicianId(value=clinician_id))
    except NoActiveRoundError:
        raise HTTPException(status_code=404, detail="No active rounding session") from None
    return _view_body(view)


@router.post("/advance", summary="Mark current patient done; return the next card")
async def advance(req: AdvanceRequest) -> dict[str, Any]:
    try:
        view = await _service().advance(
            ClinicianId(value=req.clinician_id),
            PatientId(value=req.completed_patient_id),
        )
    except NoActiveRoundError:
        raise HTTPException(status_code=404, detail="No active rounding session") from None
    if view is None:
        return {"done": True}
    return _view_body(view)
