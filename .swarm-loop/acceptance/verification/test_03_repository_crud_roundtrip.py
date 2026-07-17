"""feat_verification criterion 3 — MemoryRepository CRUD round-trip (Phase-0 tables).

Create/get accessors for source_document, document_page, extraction,
extracted_fact, guideline_document and guideline_chunk round-trip on SQLite,
exercising the JSONType columns (ocr_tokens, bbox) and the pgvector→JSON
embedding fallback. Accessor-name candidates are documented in `_W2_ACCESSORS`;
kwargs mirror the frozen Phase-0 column names. FROZEN GOAL HARNESS.
"""

from __future__ import annotations

import pytest

from ._helpers import (
    CORR,
    OCR_TOKENS,
    PNG_1PX,
    PatientArg,
    attr_from,
    call_flex,
    field,
    id_of,
    opt_field,
)

_W2_ACCESSORS = {
    ("source_document", "create"): [
        "create_source_document", "add_source_document",
        "insert_source_document", "save_source_document",
    ],
    ("source_document", "get"): ["get_source_document", "find_source_document"],
    ("document_page", "create"): [
        "create_document_page", "add_document_page",
        "insert_document_page", "save_document_page",
    ],
    ("document_page", "get"): [
        "get_document_pages", "list_document_pages", "get_document_page",
    ],
    ("extraction", "create"): [
        "create_extraction", "add_extraction", "insert_extraction", "save_extraction",
    ],
    ("extraction", "get"): ["get_extraction", "get_extractions", "list_extractions"],
    ("extracted_fact", "create"): [
        "create_extracted_fact", "add_extracted_fact",
        "insert_extracted_fact", "save_extracted_fact",
    ],
    ("extracted_fact", "get"): [
        "get_extracted_facts", "list_extracted_facts", "get_extracted_fact",
    ],
    ("guideline_document", "create"): [
        "create_guideline_document", "add_guideline_document",
        "insert_guideline_document", "save_guideline_document",
    ],
    ("guideline_document", "get"): ["get_guideline_document", "find_guideline_document"],
    ("guideline_chunk", "create"): [
        "create_guideline_chunk", "add_guideline_chunk",
        "insert_guideline_chunk", "save_guideline_chunk",
    ],
    ("guideline_chunk", "get"): [
        "get_guideline_chunks", "list_guideline_chunks", "get_guideline_chunk",
    ],
}


def _accessor(repo, entity: str, kind: str):
    names = _W2_ACCESSORS[(entity, kind)]
    fn = attr_from([repo], names)
    if fn is None:
        pytest.fail(
            f"MemoryRepository has no {kind} accessor for {entity} "
            f"(expected one of {names}) — F1 repository CRUD not implemented"
        )
    return fn


async def _created_id(session, result, what: str) -> int:
    if result is None:
        pytest.fail(f"{what}: create accessor returned None — must return the row or its id")
    v = id_of(result)
    if v is None:
        await session.flush()
        v = id_of(result)
    if v is None:
        pytest.fail(f"{what}: cannot determine the created id from {result!r}")
    return v


def _pick(rows, **match):
    if rows is None:
        return None
    if not isinstance(rows, (list, tuple)):
        rows = [rows]
    for r in rows:
        if all(opt_field(r, k, default=None) == v for k, v in match.items()):
            return r
    return rows[0] if rows else None


async def test_03_repository_crud_roundtrip(db_path):
    from copilot.memory.db import get_session_factory
    from copilot.memory.repository import MemoryRepository

    emb = [round(0.001 * i, 6) for i in range(1024)]

    async with get_session_factory()() as session:
        repo = MemoryRepository(session)

        # --- source_document ---
        doc_res = await call_flex(_accessor(repo, "source_document", "create"), [
            (("patient",), PatientArg(1001)),
            (("doc_type",), "lab_pdf"),
            (("category",), "labs"),
            (("filename", "file_name"), "crud.pdf"),
            (("content_hash", "hash"), "crud-hash-01"),
            (("openemr",), "9101"),
            (("correlation",), CORR),
            (("page_count",), 1),
            (("status",), "uploaded"),
            (("uploaded_by", "clinician"), 7),
        ], what="create_source_document")
        doc_id = await _created_id(session, doc_res, "source_document")
        got_doc = _pick(await call_flex(_accessor(repo, "source_document", "get"), [
            (("id", "document_id", "source_document_id", "document", "doc_id", "pk"), doc_id),
        ], what="get_source_document"))
        assert got_doc is not None, "get_source_document returned nothing"
        assert int(str(field(got_doc, "patient_id", "patient"))) == 1001
        assert field(got_doc, "doc_type") == "lab_pdf"
        assert field(got_doc, "correlation_id") == CORR
        assert field(got_doc, "content_hash") == "crud-hash-01"

        # --- document_page (JSONType ocr_tokens) ---
        page_res = await call_flex(_accessor(repo, "document_page", "create"), [
            (("source_document", "source_document_id", "document", "doc_id"), doc_id),
            (("page_no", "page_number", "page"), 1),
            (("image",), PNG_1PX),
            (("width",), 612),
            (("height",), 792),
            (("ocr_tokens", "tokens"), OCR_TOKENS),
        ], what="create_document_page")
        await _created_id(session, page_res, "document_page")
        page = _pick(await call_flex(_accessor(repo, "document_page", "get"), [
            (("source_document", "source_document_id", "document", "doc_id"), doc_id),
            (("page_no", "page"), 1),
        ], what="get_document_pages"), page_no=1)
        assert page is not None
        assert int(field(page, "width")) == 612 and int(field(page, "height")) == 792
        got_tokens = field(page, "ocr_tokens", "tokens")
        assert [dict(t) for t in got_tokens] == OCR_TOKENS, "JSONType ocr_tokens must round-trip"

        # --- extraction ---
        ext_res = await call_flex(_accessor(repo, "extraction", "create"), [
            (("source_document", "source_document_id", "document", "doc_id"), doc_id),
            (("schema_version", "version"), "w2-v1"),
            (("model",), "stub-vision"),
            (("confidence",), 0.91),
            (("status",), "ok"),
            (("correlation",), CORR),
        ], what="create_extraction")
        ext_id = await _created_id(session, ext_res, "extraction")
        got_ext = _pick(await call_flex(_accessor(repo, "extraction", "get"), [
            (("id", "extraction_id", "extraction", "pk"), ext_id),
            (("source_document", "source_document_id", "document", "doc_id"), doc_id),
        ], what="get_extraction"), id=ext_id)
        assert got_ext is not None
        assert field(got_ext, "schema_version") == "w2-v1"
        assert field(got_ext, "correlation_id") == CORR

        # --- extracted_fact (JSONType bbox) ---
        await call_flex(_accessor(repo, "extracted_fact", "create"), [
            (("extraction", "extraction_id"), ext_id),
            (("field_path", "field"), "hemoglobin"),
            (("value",), "13.5"),
            (("unit",), "g/dL"),
            (("reference_range",), "12.0-16.0"),
            (("abnormal",), ""),
            (("page_no", "page"), 1),
            (("bbox",), [0.1, 0.2, 0.25, 0.04]),
            (("match_confidence", "confidence", "conf"), 0.93),
            (("supported",), True),
            (("collection_date", "collected"), None),
        ], what="create_extracted_fact")
        await session.flush()
        fact = _pick(await call_flex(_accessor(repo, "extracted_fact", "get"), [
            (("extraction", "extraction_id"), ext_id),
        ], what="get_extracted_facts"), field_path="hemoglobin")
        assert fact is not None
        assert field(fact, "value") == "13.5"
        assert [float(v) for v in field(fact, "bbox")] == [0.1, 0.2, 0.25, 0.04]
        assert bool(field(fact, "supported")) is True
        assert float(field(fact, "match_confidence")) == pytest.approx(0.93)

        # --- guideline_document ---
        gd_res = await call_flex(_accessor(repo, "guideline_document", "create"), [
            (("title",), "DKA management"),
            (("source", "url"), "https://example.org/dka"),
            (("license",), "CC-BY-4.0"),
        ], what="create_guideline_document")
        gd_id = await _created_id(session, gd_res, "guideline_document")
        got_gd = _pick(await call_flex(_accessor(repo, "guideline_document", "get"), [
            (("id", "guideline_document_id", "document_id", "guideline", "pk"), gd_id),
        ], what="get_guideline_document"))
        assert got_gd is not None
        assert field(got_gd, "title") == "DKA management"
        assert field(got_gd, "license") == "CC-BY-4.0"

        # --- guideline_chunk (embedding_column JSON fallback on SQLite) ---
        await call_flex(_accessor(repo, "guideline_chunk", "create"), [
            (("guideline_document", "guideline_document_id", "document", "doc_id"), gd_id),
            (("section",), "treatment"),
            (("chunk_index", "index"), 0),
            (("content", "text"), "Begin an insulin infusion after fluid resuscitation."),
            (("embedding", "vector"), emb),
        ], what="create_guideline_chunk")
        await session.flush()
        chunk = _pick(await call_flex(_accessor(repo, "guideline_chunk", "get"), [
            (("guideline_document", "guideline_document_id", "document", "doc_id"), gd_id),
        ], what="get_guideline_chunks"), chunk_index=0)
        assert chunk is not None
        assert str(field(chunk, "content", "text")).startswith("Begin an insulin")
        got_emb = [float(v) for v in field(chunk, "embedding", "vector")]
        assert len(got_emb) == 1024 and got_emb == emb, (
            "embedding must round-trip through the SQLite JSON vector fallback"
        )
