"""Agent-served status page — health aggregates (Decision 7).

``GET /status`` (aliased ``GET /v1/status``) returns a JSON snapshot of the
agent's health, read from the agent DB and the committed eval artifacts:

- ``ingestion_count`` — documents ingested (``source_document`` rows);
- ``extraction_field_pass_rate`` — supported / total extracted fields;
- ``retrieval_hit_rate`` — **not measured**; see below;
- ``routing_decisions`` — audit-action counts (decision → count);
- ``eval_by_category`` — pass rate per *rubric* over the 53-case golden set;
- ``eval_dataset`` — which dataset those rubric numbers were captured over;
- ``latency_ms`` — p50 / p95 from the committed latency artifact;
- ``error_rate`` — failed-ingestion fraction;
- ``metric_sources`` — per-metric provenance (see next paragraph).

**Measured vs. recorded.** These aggregates do not all have the same standing,
and the difference matters to anyone reading this page as evidence: some are
computed from live agent-DB rows per request (``measured``), while others are
read back from a committed artifact captured offline (``recorded``) and do not
move when production does. Rather than leave that to the reader to guess,
``metric_sources`` labels every key with what it actually is, and any metric the
agent does not record is labelled ``unavailable`` instead of being published as
a plausible-looking number. ``retrieval_hit_rate`` is the current such case: no
per-query hit/miss telemetry is persisted, so it carries
``retrieval_hit_rate_available: false`` and a ``0.0`` placeholder that exists
only because the pinned payload contract types the key as a number — it is not
an observation that retrieval returns nothing.

No PHI: every value is a count, rate, or latency number — never a patient
identifier, document text, or extracted clinical value.

Mounted automatically by ``copilot.api.app.register_routers`` (module-level
``router``); no edit to ``app.py`` is required.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, TypeGuard

from fastapi import APIRouter
from sqlalchemy import func, select

from copilot.memory.db import session_scope
from copilot.memory.models import AuditLogRow, ExtractedFactRow, SourceDocumentRow

router = APIRouter(tags=["status"])

_logger = logging.getLogger(__name__)

# agent/copilot/api/routes/status.py -> parents[3] == agent/
_AGENT_DIR = Path(__file__).resolve().parents[3]
_GATE_BASELINE_PATH = _AGENT_DIR / "evals" / "gate_baseline.json"
_LATENCY_ARTIFACT_PATH = _AGENT_DIR / "artifacts" / "latency_report.json"


def _load_eval_aggregates() -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    """Per-rubric eval results + the dataset they were captured over.

    Reads ``evals/gate_baseline.json`` — the 53-case golden set scored against
    the five mandated rubrics (``schema_valid``, ``citation_present``,
    ``factually_consistent``, ``safe_refusal``, ``no_phi_in_logs``). This is the
    project's real eval artifact; the Week-1 ``eval_results.json`` grounding tier
    this endpoint used to read carries a different, three-category taxonomy
    (invariant / boundary / authorization) and is not the mandated rubric set.

    ``pass_rate`` is recomputed as a **fraction** (0..1) from the recorded
    passed/total counts, matching the sibling rates in the payload — the artifact
    itself stores percentages.

    Defensive: a missing or malformed artifact yields empty aggregates rather
    than raising — the status page must always answer.
    """
    try:
        raw = json.loads(_GATE_BASELINE_PATH.read_text())
    except (OSError, ValueError):
        _logger.warning("eval baseline artifact unreadable", extra={"path": str(_GATE_BASELINE_PATH)})
        return {}, {}
    if not isinstance(raw, dict):
        return {}, {}

    per_category = raw.get("per_category")
    by_category: dict[str, dict[str, float]] = {}
    if isinstance(per_category, dict):
        for name, node in per_category.items():
            if not isinstance(node, dict):
                continue
            passed = node.get("passed")
            total = node.get("total")
            if not _is_number(passed) or not _is_number(total):
                continue
            by_category[str(name)] = {
                "passed": float(passed),
                "total": float(total),
                "pass_rate": float(passed) / float(total) if float(total) else 0.0,
            }

    case_count = raw.get("case_count")
    aggregate = raw.get("pass_rate")
    dataset: dict[str, Any] = {
        "name": str(raw.get("dataset", "")),
        "case_count": int(case_count) if _is_number(case_count) else 0,
        "captured_at": str(raw.get("captured_at", "")),
        # Percent in the artifact -> fraction here, consistent with the rest.
        "pass_rate": float(aggregate) / 100.0 if _is_number(aggregate) else 0.0,
    }
    return by_category, dataset


def _is_number(value: object) -> TypeGuard[int | float]:
    """A real int/float — bools are ints in Python and are not numbers here.

    A ``TypeGuard`` rather than a bare ``bool`` so the narrowing is visible to
    the type checker: callers get ``int | float`` out of an untyped JSON read
    without casting a possibly-``None`` value.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: What every published aggregate actually IS — the honesty contract of this
#: endpoint. ``measured`` = computed from live agent-DB rows at request time;
#: ``recorded`` = read from a committed artifact (a captured baseline, NOT live
#: telemetry — it does not move when production does); ``unavailable`` = the
#: agent does not record the signal, so no number is published.
_METRIC_SOURCES: dict[str, str] = {
    "ingestion_count": "measured: agent DB, source_document rows",
    "extraction_field_pass_rate": "measured: agent DB, extracted_fact.supported / total",
    "retrieval_hit_rate": (
        "unavailable: retrieval outcomes are not recorded — no per-query hit/miss "
        "telemetry is persisted and the eval gate scores recorded fixtures rather "
        "than exercising the retriever. The published 0.0 is a contract placeholder, "
        "not a measurement of zero hits; ignore it and read this field instead."
    ),
    "routing_decisions": "measured: agent DB, audit_log.action counts",
    "eval_by_category": (
        "recorded: evals/gate_baseline.json — the 53-case golden set scored on the "
        "five rubrics (schema_valid, citation_present, factually_consistent, "
        "safe_refusal, no_phi_in_logs) by the deterministic LLM-free gate"
    ),
    "eval_dataset": "recorded: evals/gate_baseline.json provenance block",
    "latency_ms": (
        "recorded: artifacts/latency_report.json — a committed, stubbed, LLM-free "
        "baseline captured by scripts/latency_report.py. NOT live production "
        "telemetry; live p95 comes from the doc.ingest / guideline.retrieve spans "
        "in Langfuse (OBSERVABILITY.md §7)"
    ),
    "error_rate": "measured: agent DB, source_document.status == 'failed' / total",
}


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
    eval_by_category, eval_dataset = _load_eval_aggregates()

    return {
        "ingestion_count": ingestion_count,
        "extraction_field_pass_rate": extraction_field_pass_rate,
        # Not measured — see `retrieval_hit_rate_available` / `metric_sources`.
        # The 0.0 is a placeholder the pinned payload contract requires (the
        # key is typed as a number), NOT an observation that retrieval missed.
        "retrieval_hit_rate": 0.0,
        "retrieval_hit_rate_available": False,
        "routing_decisions": routing_decisions,
        "eval_by_category": eval_by_category,
        "eval_dataset": eval_dataset,
        "latency_ms": _load_latency_ms(),
        "error_rate": error_rate,
        "metric_sources": _METRIC_SOURCES,
    }


@router.get("/status", summary="Agent health aggregates (status page)")
async def status_page() -> dict[str, Any]:
    return await _status_payload()


@router.get("/v1/status", summary="Agent health aggregates (status page, v1 alias)")
async def status_page_v1() -> dict[str, Any]:
    return await _status_payload()
