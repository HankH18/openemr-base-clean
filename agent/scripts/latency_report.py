#!/usr/bin/env python3
"""SLO latency report — stubbed, LLM-free p50/p95 for the Week-2 hot paths.

Runs the two latency-critical pipelines end-to-end against the deterministic
keyless stubs (no Anthropic / Voyage / Cohere key required) and writes a JSON
artifact carrying numeric ``p50``/``p95`` (milliseconds) for:

- **document ingestion** (``doc_ingestion``) — upload → rasterize → OCR →
  extract → reconcile → persist, via the derived-only uploader so it needs no
  OpenEMR write surface;
- **evidence retrieval** (``evidence_retrieval``) — hybrid sparse+dense guideline
  retrieval + rerank over a small seeded corpus.

The report feeds the OBSERVABILITY.md SLO targets and the agent status page.
It makes NO pass/fail judgement on the numbers — it just measures.

Usage (from the ``agent/`` directory)::

    python scripts/latency_report.py --out artifacts/latency_report.json [--samples 5]

Deterministic + isolated: points the agent at a throwaway temp SQLite file so a
run never touches a real database, and uses the keyless stubs so it never makes
a network call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Make ``import copilot`` resolve to THIS checkout when invoked by path
# (sys.path[0] is scripts/, not the agent/ dir that holds the package).
_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


def _minimal_pdf(text: str = "Hemoglobin 13.5 g/dL") -> bytes:
    """A deterministic, valid, single-page PDF (byte-stable; no dependency)."""
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in [0, 100]); 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * frac


def _summarize(samples_ms: list[float]) -> dict[str, float | int]:
    return {
        "p50": round(_percentile(samples_ms, 50), 3),
        "p95": round(_percentile(samples_ms, 95), 3),
        "min": round(min(samples_ms), 3) if samples_ms else 0.0,
        "max": round(max(samples_ms), 3) if samples_ms else 0.0,
        "n": len(samples_ms),
    }


async def _seed_guideline_corpus() -> None:
    """A tiny guideline corpus so retrieval exercises the real fusion+rerank path."""
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository
    from copilot.rag.embeddings import build_embedder

    embedder = build_embedder(get_settings())
    chunks = [
        ("Potassium 5.5-6.0 mEq/L is moderate hyperkalemia; recheck and treat if symptomatic.",
         "hyperkalemia"),
        ("ECG changes with hyperkalemia warrant urgent calcium gluconate.", "hyperkalemia"),
        ("Troponin elevation should be trended over serial draws.", "acs"),
    ]
    async with session_scope() as session:
        repo = MemoryRepository(session)
        doc = await repo.create_guideline_document(title="Seed guideline", source="latency-seed")
        for index, (content, section) in enumerate(chunks):
            await repo.create_guideline_chunk(
                guideline_document_id=doc.id,
                content=content,
                section=section,
                chunk_index=index,
                embedding=embedder.embed([content])[0],
            )


async def _measure(samples: int) -> dict[str, Any]:
    from copilot.config import get_settings
    from copilot.documents import DerivedOnlyUploader, DocumentIngestionService
    from copilot.domain.primitives import PatientId
    from copilot.memory.db import Base, get_engine
    from copilot.rag import build_retriever

    # Test/dev convenience — create the schema on the throwaway SQLite file.
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = get_settings()
    pdf = _minimal_pdf()

    await _seed_guideline_corpus()

    ingest_ms: list[float] = []
    for i in range(samples):
        service = DocumentIngestionService(
            settings, write_client_factory=lambda: DerivedOnlyUploader()
        )
        started = time.perf_counter()
        await service.attach_and_extract(
            patient_id=PatientId(value=100_000 + i),
            content=pdf,
            doc_type="lab_pdf",
            correlation_id=f"latency-ingest-{i}",
        )
        ingest_ms.append((time.perf_counter() - started) * 1000.0)

    retriever = build_retriever(settings)
    retrieve_ms: list[float] = []
    for _ in range(samples):
        started = time.perf_counter()
        await retriever.retrieve("hyperkalemia potassium management")
        retrieve_ms.append((time.perf_counter() - started) * 1000.0)

    await get_engine().dispose()

    return {
        "unit": "milliseconds",
        "samples": samples,
        "doc_ingestion": _summarize(ingest_ms),
        "evidence_retrieval": _summarize(retrieve_ms),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Stubbed p50/p95 latency SLO report.")
    parser.add_argument("--out", type=Path, required=True, help="Path to write the JSON report.")
    parser.add_argument("--samples", type=int, default=5, help="Samples per pipeline (default 5).")
    args = parser.parse_args()

    # Isolate on a throwaway SQLite file — deterministic, never touches a real DB.
    tmp_dir = tempfile.mkdtemp(prefix="latency-report-")
    db_file = Path(tmp_dir) / "latency.db"
    os.environ["COPILOT_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"

    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    report = asyncio.run(_measure(max(args.samples, 1)))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    print(f"latency report written to {args.out}")


if __name__ == "__main__":
    main()
