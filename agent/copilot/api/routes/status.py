"""Agent-served status page — health aggregates (Decision 7).

``GET /status`` (aliased ``GET /v1/status``) returns a JSON snapshot of the
agent's health, read from the agent DB and the committed eval artifacts:

- ``ingestion_count`` — documents ingested (``source_document`` rows);
- ``extraction_field_pass_rate`` — supported / total extracted fields;
- ``retrieval_hit_rate`` — retrieval-eval pass rate;
- ``routing_decisions`` — audit-action counts (decision → count);
- ``eval_by_category`` — eval pass-rate per category;
- ``latency_ms`` — p50 / p95 (from the latency artifact when present);
- ``error_rate`` — failed-ingestion fraction.

No PHI: every value is a count, rate, or latency number — never a patient
identifier, document text, or extracted clinical value.

Mounted automatically by ``copilot.api.app.register_routers`` (module-level
``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select

from copilot.memory.db import session_scope
from copilot.memory.models import AuditLogRow, ExtractedFactRow, SourceDocumentRow

router = APIRouter(tags=["status"])

_logger = logging.getLogger(__name__)

# agent/copilot/api/routes/status.py -> parents[3] == agent/
_AGENT_DIR = Path(__file__).resolve().parents[3]
_EVAL_RESULTS_PATH = _AGENT_DIR / "evals" / "eval_results.json"
_LATENCY_ARTIFACT_PATH = _AGENT_DIR / "artifacts" / "latency_report.json"


def _load_eval_aggregates() -> tuple[dict[str, dict[str, float]], float]:
    """Per-category eval pass-rate + the retrieval-eval hit rate.

    Defensive: a missing or malformed artifact yields empty aggregates and a
    ``0.0`` retrieval hit rate rather than raising — the status page must always
    answer.
    """
    try:
        raw = json.loads(_EVAL_RESULTS_PATH.read_text())
    except (OSError, ValueError):
        return {}, 0.0

    cases = raw.get("cases") if isinstance(raw, dict) else None
    if not isinstance(cases, list):
        return {}, 0.0

    by_category: dict[str, dict[str, float]] = {}
    retrieval_passed = 0
    retrieval_total = 0
    for case in cases:
        if not isinstance(case, dict):
            continue
        category = str(case.get("category", "uncategorized"))
        passed = bool(case.get("passed"))
        bucket = by_category.setdefault(category, {"passed": 0.0, "total": 0.0, "pass_rate": 0.0})
        bucket["total"] += 1
        if passed:
            bucket["passed"] += 1
        kind = str(case.get("kind", ""))
        if "retriev" in category.lower() or "retriev" in kind.lower():
            retrieval_total += 1
            retrieval_passed += 1 if passed else 0

    for bucket in by_category.values():
        bucket["pass_rate"] = bucket["passed"] / bucket["total"] if bucket["total"] else 0.0

    retrieval_hit_rate = retrieval_passed / retrieval_total if retrieval_total else 0.0
    return by_category, retrieval_hit_rate


def _load_latency_ms() -> dict[str, float]:
    """p50/p95 latency from the committed artifact; zeros when none is present."""
    try:
        raw = json.loads(_LATENCY_ARTIFACT_PATH.read_text())
    except (OSError, ValueError):
        return {"p50": 0.0, "p95": 0.0}

    if not isinstance(raw, dict):
        return {"p50": 0.0, "p95": 0.0}
    for node in raw.values():
        if isinstance(node, dict):
            p50 = node.get("p50")
            p95 = node.get("p95")
            if isinstance(p50, (int, float)) and isinstance(p95, (int, float)):
                return {"p50": float(p50), "p95": float(p95)}
    return {"p50": 0.0, "p95": 0.0}


async def _status_payload() -> dict[str, Any]:
    async with session_scope() as session:
        ingestion_count = int(
            (await session.execute(select(func.count()).select_from(SourceDocumentRow)))
            .scalar_one()
            or 0
        )
        failed_count = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(SourceDocumentRow)
                    .where(SourceDocumentRow.status == "failed")
                )
            ).scalar_one()
            or 0
        )
        total_fields = int(
            (await session.execute(select(func.count()).select_from(ExtractedFactRow)))
            .scalar_one()
            or 0
        )
        supported_fields = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(ExtractedFactRow)
                    .where(ExtractedFactRow.supported.is_(True))
                )
            ).scalar_one()
            or 0
        )
        action_rows = (
            await session.execute(
                select(AuditLogRow.action, func.count()).group_by(AuditLogRow.action)
            )
        ).all()

    routing_decisions = {str(action): int(count) for action, count in action_rows}
    extraction_field_pass_rate = supported_fields / total_fields if total_fields else 0.0
    error_rate = failed_count / ingestion_count if ingestion_count else 0.0
    eval_by_category, retrieval_hit_rate = _load_eval_aggregates()

    return {
        "ingestion_count": ingestion_count,
        "extraction_field_pass_rate": extraction_field_pass_rate,
        "retrieval_hit_rate": retrieval_hit_rate,
        "routing_decisions": routing_decisions,
        "eval_by_category": eval_by_category,
        "latency_ms": _load_latency_ms(),
        "error_rate": error_rate,
    }


@router.get("/status", summary="Agent health aggregates (status page)")
async def status_page() -> dict[str, Any]:
    return await _status_payload()


@router.get("/v1/status", summary="Agent health aggregates (status page, v1 alias)")
async def status_page_v1() -> dict[str, Any]:
    return await _status_payload()
