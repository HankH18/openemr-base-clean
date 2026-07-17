"""Black-box helpers for the frozen Week-2 acceptance suite (per-feature copy).

FROZEN GOAL HARNESS — do not edit to make a test pass.

Defensive-import discipline: every entry into not-yet-built feature surface goes
through ``require_any_module`` / ``require_attr_any`` / ``enum_member_or_fail`` /
the ``make_*`` builders below, which turn a missing surface into ``pytest.fail``
— a test that RAN and FAILED — never an import/collection error (run.py's
contract: an all-failing suite prints 0 and exits 0).

Where the intended surface leaves naming latitude (W2_ARCHITECTURE.md and
.swarm-loop/backlog.md pin modules + semantics, not every parameter name), the
``bind_kwargs``/``call_flex`` binder maps semantic values onto a callable's
actual parameter names (exact-name match first, then substrings of length >= 4).
The *semantics* asserted by the tests are frozen; implementations keep naming
latitude within the candidate lists documented here.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

ACC_DIR = Path(__file__).resolve().parents[1]
AGENT_DIR = ACC_DIR.parents[1] / "agent"
for _p in (str(ACC_DIR), str(AGENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A valid CorrelationId (8..64 chars of [A-Za-z0-9_-]).
CORR = "acceptance-w2-0001"

# Word-level OCR fixture tokens (normalized [x, y, w, h] bboxes).
OCR_TOKENS: list[dict[str, Any]] = [
    {"text": "Hemoglobin", "bbox": [0.10, 0.10, 0.20, 0.03], "conf": 0.98},
    {"text": "13.5", "bbox": [0.32, 0.10, 0.06, 0.03], "conf": 0.97},
    {"text": "g/dL", "bbox": [0.40, 0.10, 0.06, 0.03], "conf": 0.96},
]

# The documented ExtractedFact field set (mirrors the frozen Phase-0 columns).
VALID_FACT_PAYLOAD: dict[str, Any] = {
    "field_path": "hemoglobin",
    "value": "13.5",
    "unit": "g/dL",
    "page_no": 1,
    "bbox": [0.1, 0.2, 0.25, 0.04],
    "match_confidence": 0.9,
    "supported": True,
}

# A canonical 1x1 PNG (opaque bytes passed around; stubs never decode it).
PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# --- defensive import / lookup ----------------------------------------------


def import_candidates(*names: str) -> list[Any]:
    """Import every candidate module that exists; silently skip the rest."""
    mods = []
    for name in names:
        try:
            mods.append(importlib.import_module(name))
        except Exception:
            continue
    return mods


def require_any_module(*names: str, what: str) -> list[Any]:
    mods = import_candidates(*names)
    if not mods:
        pytest.fail(
            f"{what}: none of the candidate modules exist yet "
            f"({', '.join(names)}) — feature not implemented"
        )
    return mods


def attr_from(objs: Any, names: list[str]) -> Any:
    """First attribute found under any candidate name on any candidate object."""
    if not isinstance(objs, (list, tuple)):
        objs = [objs]
    for obj in objs:
        for n in names:
            if hasattr(obj, n):
                return getattr(obj, n)
    return None


def require_attr_any(objs: Any, names: list[str], what: str) -> Any:
    got = attr_from(objs, names)
    if got is None:
        pytest.fail(f"{what}: no candidate attribute {names!r} found — feature not implemented")
    return got


def enum_member_or_fail(enum_cls: Any, value: str, what: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        pytest.fail(
            f"{what}: {enum_cls.__name__} has no member {value!r} — feature not implemented"
        )


async def resolve(x: Any) -> Any:
    if inspect.isawaitable(x):
        return await x
    return x


def field(obj: Any, *names: str, what: str = "object") -> Any:
    """Mapping key or attribute under any candidate name — pytest.fail if none."""
    for n in names:
        if isinstance(obj, Mapping) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    pytest.fail(f"{what} exposes none of {names!r} (got {type(obj).__name__})")


def opt_field(obj: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        if isinstance(obj, Mapping) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def id_of(result: Any) -> int | None:
    """Best-effort integer id of a create-accessor result (id or row/DTO)."""
    if isinstance(result, bool):
        return None
    if isinstance(result, int):
        return result
    if isinstance(result, str) and result.strip().isdigit():
        return int(result)
    v = opt_field(result, "id", default=None)
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v)
    return None


# --- adaptive argument binding ----------------------------------------------


class PatientArg:
    """Dual-typed patient id: PatientId when annotated, plain int otherwise."""

    def __init__(self, pid: int) -> None:
        self.pid = pid


class ClinicianArg:
    def __init__(self, cid: int) -> None:
        self.cid = cid


def _adapt(value: Any, param: inspect.Parameter, tmp_path: Any) -> Any:
    ann = "" if param.annotation is inspect.Parameter.empty else str(param.annotation)
    if isinstance(value, PatientArg):
        if "PatientId" in ann:
            from copilot.domain.primitives import PatientId

            return PatientId(value=value.pid)
        return value.pid
    if isinstance(value, ClinicianArg):
        if "ClinicianId" in ann:
            from copilot.domain.primitives import ClinicianId

            return ClinicianId(value=value.cid)
        return value.cid
    if isinstance(value, bytes) and "path" in param.name.lower() and tmp_path is not None:
        p = Path(tmp_path) / f"bind_{param.name}.bin"
        p.write_bytes(value)
        return str(p)
    return value


def bind_kwargs(
    fn: Any,
    semantic: list[tuple[tuple[str, ...], Any]],
    *,
    tmp_path: Any = None,
    what: str | None = None,
) -> dict[str, Any]:
    """Map semantic (patterns, value) pairs onto ``fn``'s parameters by name.

    Two passes per parameter: exact pattern==name first, then substring matching
    for patterns of length >= 4 (short generic patterns like "id" only ever match
    exactly). A *required* parameter no semantic entry can satisfy is a frozen-
    surface violation -> pytest.fail with the parameter named.
    """
    what = what or getattr(fn, "__qualname__", repr(fn))
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        pytest.fail(f"{what}: signature is not introspectable")
    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        low = name.lower()
        matched = False
        for patterns, value in semantic:  # pass 1: exact
            if any(p == low for p in patterns):
                kwargs[name] = _adapt(value, param, tmp_path)
                matched = True
                break
        if not matched:
            for patterns, value in semantic:  # pass 2: substring (len >= 4)
                if any(len(p) >= 4 and p in low for p in patterns):
                    kwargs[name] = _adapt(value, param, tmp_path)
                    matched = True
                    break
        if not matched and param.default is inspect.Parameter.empty:
            pytest.fail(
                f"{what}: required parameter {name!r} is outside the frozen acceptance "
                "surface — give it a default or use a documented parameter name"
            )
    return kwargs


async def call_flex(
    fn: Any,
    semantic: list[tuple[tuple[str, ...], Any]],
    *,
    tmp_path: Any = None,
    what: str | None = None,
) -> Any:
    return await resolve(fn(**bind_kwargs(fn, semantic, tmp_path=tmp_path, what=what)))


def instantiate_flex(cls: Any, semantic: list[tuple[tuple[str, ...], Any]]) -> Any:
    return cls(**bind_kwargs(cls, semantic, what=f"{cls.__name__}()"))


# --- deterministic fixture PDF ------------------------------------------------


def build_fixture_pdf(texts: tuple[str, ...] = ("Hemoglobin 13.5 g/dL",)) -> bytes:
    """A minimal, deterministic, valid multi-page PDF (one text line per page)."""
    n = len(texts)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(texts):
        cid = 5 + 2 * i
        objs.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {cid} 0 R >>"
            ).encode()
        )
        stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --- citation-union builders (defensive) --------------------------------------

_CITATION_MODULES = (
    "copilot.domain.primitives",
    "copilot.domain.documents",
    "copilot.domain.contracts",
    "copilot.domain.citations",
)


def citation_classes() -> tuple[Any, Any]:
    mods = import_candidates(*_CITATION_MODULES)
    doc = attr_from(mods, ["DocumentCitation"])
    gl = attr_from(mods, ["GuidelineCitation"])
    if doc is None or gl is None:
        pytest.fail(
            "Week-2 citation union (DocumentCitation/GuidelineCitation) not implemented — "
            f"looked in {_CITATION_MODULES}"
        )
    return doc, gl


def make_document_citation(
    *,
    source_id: Any,
    page: int,
    fact_id: Any,
    value: str,
    bbox: list[float],
    confidence: float,
) -> Any:
    doc_cls, _ = citation_classes()
    try:
        return doc_cls(
            source_type="document",
            source_id=str(source_id),
            page_or_section=page,
            field_or_chunk_id=str(fact_id),
            quote_or_value=str(value),
            bbox=list(bbox),
            confidence=confidence,
        )
    except Exception as exc:
        pytest.fail(f"DocumentCitation rejects the W2_ARCHITECTURE field set: {exc}")


def make_guideline_citation(*, source_id: Any, section: str, chunk_id: Any, quote: str) -> Any:
    _, gl_cls = citation_classes()
    try:
        return gl_cls(
            source_type="guideline",
            source_id=str(source_id),
            page_or_section=section,
            field_or_chunk_id=str(chunk_id),
            quote_or_value=quote,
        )
    except Exception as exc:
        pytest.fail(f"GuidelineCitation rejects the W2_ARCHITECTURE field set: {exc}")


def make_claim(text: str, source_ref: Any) -> Any:
    from copilot.domain.contracts import Claim

    try:
        return Claim(text=text, source_ref=source_ref)
    except Exception as exc:
        pytest.fail(f"Claim.source_ref does not accept the Week-2 citation union yet: {exc}")


# --- strict extraction schemas -------------------------------------------------

_SCHEMA_MODULES = (
    "copilot.domain.documents",
    "copilot.documents.schemas",
    "copilot.domain.contracts",
    "copilot.documents",
)


def schema_class(name: str) -> Any:
    mods = require_any_module(*_SCHEMA_MODULES, what=f"strict extraction schema {name}")
    cls = attr_from(mods, [name])
    if cls is None:
        pytest.fail(
            f"strict extraction schema {name} not found in any of {_SCHEMA_MODULES} — "
            "F1 schemas not implemented"
        )
    return cls


# --- serve-time verification (verify_answer) ----------------------------------


class MappingReader:
    """In-memory ResourceReader: serves the given raw FHIR resources by id."""

    def __init__(self, resources: list[Mapping[str, Any]]) -> None:
        self._by_key = {
            (str(r.get("resourceType")), str(r.get("id"))): dict(r) for r in resources
        }

    async def read(self, resource_type: Any, resource_id: str) -> dict[str, Any]:
        rt = getattr(resource_type, "value", resource_type)
        res = self._by_key.get((str(rt), str(resource_id)))
        if res is None:
            raise LookupError(f"no such resource {rt}/{resource_id}")
        return res


class FailingReader:
    """A reader that always raises — document/guideline grounding must not need FHIR."""

    async def read(self, resource_type: Any, resource_id: str) -> dict[str, Any]:
        raise LookupError("this test provides no FHIR resources")


async def run_verify(claims: list[Any], patient_pid: int, reader: Any) -> Any:
    import copilot.verification.serve as serve

    fn = require_attr_any(
        [serve], ["verify_answer", "verify_claims", "verify"], what="serve-time verifier"
    )
    semantic = [
        (("claims", "answer"), list(claims)),
        (("patient",), PatientArg(patient_pid)),
        (("fhir", "reader", "client"), reader),
    ]
    return await call_flex(fn, semantic, what="verify_answer")


def action_of(result: Any) -> str:
    a = field(result, "action", what="VerificationResult")
    return str(getattr(a, "value", a))


def passing_claims(result: Any) -> list[Any]:
    out = []
    for c in field(result, "claims", what="VerificationResult"):
        if bool(opt_field(c, "attribution_ok", default=False)) and bool(
            opt_field(c, "value_match", default=False)
        ):
            out.append(c)
    return out


# --- direct DB seeding / inspection (frozen Phase-0 models = stable infra) -----


def _open_sync(db_path: Any) -> tuple[Any, Any]:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    eng = sa.create_engine(f"sqlite:///{db_path}")
    return eng, Session(eng, expire_on_commit=False)


def insert_rows(db_path: Any, *rows: Any) -> None:
    eng, s = _open_sync(db_path)
    try:
        s.add_all(rows)
        s.flush()
        s.commit()
    finally:
        s.close()
        eng.dispose()


def fetch_rows(db_path: Any, model: Any, **where: Any) -> list[Any]:
    import sqlalchemy as sa
    from sqlalchemy.orm import Session

    eng = sa.create_engine(f"sqlite:///{db_path}")
    try:
        with Session(eng) as s:
            stmt = sa.select(model)
            for k, v in where.items():
                stmt = stmt.where(getattr(model, k) == v)
            rows = list(s.execute(stmt).scalars())
            s.expunge_all()
            return rows
    finally:
        eng.dispose()


def seed_document_fact(
    db_path: Any,
    *,
    patient_id: int = 1001,
    value: str = "13.5",
    field_path: str = "hemoglobin",
    unit: str = "g/dL",
    page_no: int = 1,
    bbox: list[float] | None = None,
    match_confidence: float | None = 0.95,
    supported: bool = True,
    correlation_id: str = CORR,
) -> tuple[int, int, int]:
    """Seed source_document -> extraction -> extracted_fact; return their ids."""
    from copilot.memory.models import ExtractedFactRow, ExtractionRow, SourceDocumentRow

    if bbox is None and supported:
        bbox = [0.10, 0.20, 0.25, 0.04]
    eng, s = _open_sync(db_path)
    try:
        doc = SourceDocumentRow(
            patient_id=patient_id,
            doc_type="lab_pdf",
            status="extracted",
            openemr_document_id="9001",
            content_hash=f"acc-hash-{patient_id}-{field_path}",
            page_count=1,
            filename="seed.pdf",
            correlation_id=correlation_id,
        )
        s.add(doc)
        s.flush()
        ext = ExtractionRow(
            source_document_id=doc.id,
            schema_version="w2-v1",
            model="stub-vision",
            confidence_overall=0.9,
            status="ok",
            correlation_id=correlation_id,
        )
        s.add(ext)
        s.flush()
        fact = ExtractedFactRow(
            extraction_id=ext.id,
            field_path=field_path,
            value=value,
            unit=unit,
            page_no=page_no,
            bbox=list(bbox) if bbox is not None else None,
            match_confidence=match_confidence,
            supported=supported,
        )
        s.add(fact)
        s.flush()
        s.commit()
        return doc.id, ext.id, fact.id
    finally:
        s.close()
        eng.dispose()


def seed_guideline_chunk(
    db_path: Any,
    *,
    content: str,
    section: str = "dka-treatment",
    title: str = "DKA management guideline",
) -> tuple[int, int]:
    from copilot.memory.models import GuidelineChunkRow, GuidelineDocumentRow

    eng, s = _open_sync(db_path)
    try:
        gdoc = GuidelineDocumentRow(title=title, source="https://example.org/dka", license="CC-BY-4.0")
        s.add(gdoc)
        s.flush()
        chunk = GuidelineChunkRow(
            guideline_document_id=gdoc.id,
            section=section,
            chunk_index=0,
            content=content,
            embedding=None,
        )
        s.add(chunk)
        s.flush()
        s.commit()
        return gdoc.id, chunk.id
    finally:
        s.close()
        eng.dispose()


def audit_entries(db_path: Any, *, patient_id: int | None = None, action_contains: str | None = None) -> list[Any]:
    from copilot.memory.models import AuditLogRow

    out = []
    for r in fetch_rows(db_path, AuditLogRow):
        if patient_id is not None and r.patient_id != patient_id:
            continue
        if action_contains is not None and action_contains not in (r.action or ""):
            continue
        out.append(r)
    return out


# --- OpenEMR write path (fake-backed) ------------------------------------------


def far_future_token() -> Any:
    from datetime import UTC, datetime

    from copilot.fhir.auth import OAuthToken

    return OAuthToken(
        access_token="fake-acceptance-token",
        token_type="Bearer",
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def make_write_service(settings: Any) -> Any:
    import _fake_openemr as fake
    from copilot.fhir.auth import StaticTokenProvider
    from copilot.fhir.provider import build_fhir_client
    from copilot.fhir.write_client import OpenEmrWriteClient
    from copilot.writeback.service import IdempotencyStore, WriteService

    provider = StaticTokenProvider(token=far_future_token())
    return WriteService(
        settings,
        idempotency=IdempotencyStore(),
        write_client_factory=lambda: OpenEmrWriteClient(fake.STANDARD_API_BASE_URL, provider),
        read_client_factory=lambda: build_fhir_client(settings),
    )


def write_calls(fake: Any, resource: str) -> list[dict[str, Any]]:
    return [c for c in fake.WRITE_CALLS if c.get("resource") == resource]


async def propose_flex(
    service: Any,
    *,
    kind_value: str,
    raw_value: str,
    patient_pid: int,
    clinician_id: int = 7,
) -> tuple[Any, str]:
    """Agent-proposed propose step. Requires the promoted write kind AND an
    agent-proposed path (an ``entry_mode``-accepting propose, or a dedicated
    ``propose_agent*`` method). Returns (ProposedWrite, idempotency_key)."""
    from copilot.domain.writes import WriteEntryMode, WriteKind

    kind = enum_member_or_fail(WriteKind, kind_value, what="promoted write-back kinds (F4b)")
    mode = enum_member_or_fail(
        WriteEntryMode, "agent_proposed_physician_confirmed", what="agent-proposed entry mode"
    )
    chosen = None
    for name in ("propose_agent_write", "propose_agent", "agent_propose", "propose"):
        fn = getattr(service, name, None)
        if fn is None:
            continue
        params = inspect.signature(fn).parameters
        if name != "propose" or any(("entry_mode" in p) or (p == "mode") for p in params):
            chosen = fn
            break
    if chosen is None:
        pytest.fail(
            "propose→confirm gate has no agent-proposed path: WriteService.propose accepts "
            "no entry_mode and no propose_agent* method exists — F4b not implemented"
        )
    semantic = [
        (("clinician",), ClinicianArg(clinician_id)),
        (("patient",), PatientArg(patient_pid)),
        (("kind",), kind),
        (("entry_mode", "mode"), mode),
        (("raw_value", "value", "title", "text", "problem", "allergy", "substance"), raw_value),
    ]
    result = await call_flex(chosen, semantic, what=f"WriteService.{chosen.__name__}")
    if isinstance(result, tuple) and len(result) == 2:
        proposed, key = result
    else:
        proposed = result
        key = field(
            field(proposed, "candidate", what="ProposedWrite"),
            "idempotency_key",
            what="WriteCandidate",
        )
    return proposed, str(key)


async def commit_flex(
    service: Any,
    *,
    proposed: Any,
    key: str,
    patient_pid: int,
    clinician_id: int = 7,
) -> Any:
    candidate = field(proposed, "candidate", what="ProposedWrite")
    fn = require_attr_any(
        [service], ["commit", "confirm", "confirm_and_commit"], what="write-back commit step"
    )
    semantic = [
        (("clinician",), ClinicianArg(clinician_id)),
        (("patient",), PatientArg(patient_pid)),
        (("candidate", "proposed", "write"), candidate),
        (("idempotency", "key"), key),
    ]
    return await call_flex(fn, semantic, what="WriteService.commit")


async def upload_flex(
    client: Any,
    *,
    patient_pid: int,
    content: bytes,
    filename: str = "upload.pdf",
) -> Any:
    """Call the write client's document upload with the frozen semantics."""
    fn = require_attr_any(
        [client],
        ["upload_document", "upload_source_document", "create_document"],
        what="OpenEmrWriteClient.upload_document (F4a)",
    )
    semantic = [
        (("filename", "file_name"), filename),
        (("patient", "pid"), PatientArg(patient_pid)),
        (("category", "path"), "acceptance-docs"),
        (("mime", "content_type", "media_type"), "application/pdf"),
        (("doc_type", "kind", "type"), "lab_pdf"),
        (("idempotency",), "acc-idem-0001"),
        (("correlation",), CORR),
        (("content", "data", "bytes", "file", "blob", "document", "payload"), content),
    ]
    return await call_flex(fn, semantic, what="upload_document")


# --- document pipeline resolvers -----------------------------------------------

_DOC_MODULES = (
    "copilot.documents.service",
    "copilot.documents.pipeline",
    "copilot.documents.ingest",
    "copilot.documents.ingestion",
    "copilot.documents",
)


def resolve_attach(settings: Any, tmp_path: Any) -> Any:
    """Locate ``attach_and_extract`` (module function or service-class method)
    under copilot.documents.* and return an async wrapper with a fixed call shape.
    """
    mods = require_any_module(*_DOC_MODULES, what="document ingestion pipeline (attach_and_extract)")
    target = attr_from(mods, ["attach_and_extract"])
    if target is None:
        owner = None
        for mod in mods:
            for name in dir(mod):
                obj = getattr(mod, name)
                if (
                    inspect.isclass(obj)
                    and obj.__module__.startswith("copilot.documents")
                    and callable(getattr(obj, "attach_and_extract", None))
                ):
                    owner = obj
                    break
            if owner is not None:
                break
        if owner is None:
            pytest.fail(
                "attach_and_extract not found in copilot.documents.* "
                "(neither a module function nor a service-class method) — F3 not implemented"
            )
        instance = instantiate_flex(owner, [(("settings", "config"), settings)])
        target = instance.attach_and_extract

    async def attach(
        *,
        patient_pid: int,
        content: bytes,
        doc_type: str = "lab_pdf",
        filename: str = "acceptance.pdf",
        correlation_id: str = CORR,
    ) -> Any:
        semantic = [
            (("correlation",), correlation_id),
            (("patient",), PatientArg(patient_pid)),
            (("filename", "file_name"), filename),
            (("doc_type", "kind", "type"), doc_type),
            (("mime", "content_type", "media_type"), "application/pdf"),
            (("content", "data", "bytes", "file", "blob", "payload", "document"), content),
            (("settings", "config"), settings),
        ]
        return await call_flex(target, semantic, tmp_path=tmp_path, what="attach_and_extract")

    return attach


_RASTER_MODULES = (
    "copilot.documents.raster",
    "copilot.documents.rasterize",
    "copilot.documents.pdf",
    "copilot.documents.pages",
    "copilot.documents.pipeline",
    "copilot.documents",
)
_RASTER_NAMES = [
    "rasterize_pdf",
    "rasterize",
    "render_pdf_pages",
    "render_pages",
    "pdf_to_page_images",
    "pdf_to_images",
    "raster_pages",
]


def resolve_raster() -> Any:
    mods = require_any_module(*_RASTER_MODULES, what="PDF rasterization (pypdfium2)")
    fn = attr_from(mods, _RASTER_NAMES)
    if fn is None:
        pytest.fail(
            f"rasterizer not found: expected one of {_RASTER_NAMES} in copilot.documents.* — "
            "F3 not implemented"
        )
    return fn


async def raster_pages(fn: Any, pdf_bytes: bytes, tmp_path: Any) -> list[Any]:
    semantic = [
        (("content", "data", "bytes", "file", "blob", "document"), pdf_bytes),
        (("pdf",), pdf_bytes),
    ]
    pages = await call_flex(fn, semantic, tmp_path=tmp_path, what="rasterize_pdf")
    if not isinstance(pages, (list, tuple)):
        pytest.fail(f"rasterizer must return a list of pages (got {type(pages).__name__})")
    return list(pages)


def page_geometry(page: Any) -> tuple[int, int, bytes]:
    """(width, height, image_bytes) of one rasterized page (attr/mapping/PIL)."""
    size = opt_field(page, "size", default=None)
    if size is not None and hasattr(page, "tobytes"):
        w, h = size
        return int(w), int(h), page.tobytes()
    w = field(page, "width", "w", what="rasterized page")
    h = field(page, "height", "h", what="rasterized page")
    img = field(page, "image", "png", "image_bytes", "data", "content", what="rasterized page")
    if hasattr(img, "tobytes"):
        img = img.tobytes()
    return int(w), int(h), bytes(img)


_OCR_MODULES = ("copilot.documents.ocr", "copilot.documents.pipeline", "copilot.documents")


def resolve_ocr_module() -> list[Any]:
    return require_any_module(*_OCR_MODULES, what="OCR Protocol (StubOcr/build_ocr)")


def normalize_tokens(result: Any) -> list[tuple[str, list[float], float]]:
    tokens = result
    if isinstance(result, Mapping) and "tokens" in result:
        tokens = result["tokens"]
    elif not isinstance(result, (list, tuple)) and hasattr(result, "tokens"):
        tokens = result.tokens
    if not isinstance(tokens, (list, tuple)):
        pytest.fail(f"OCR result is not a token list (got {type(result).__name__})")
    out = []
    for t in tokens:
        text = field(t, "text", "word", what="OCR token")
        bbox = field(t, "bbox", "box", what="OCR token")
        conf = field(t, "conf", "confidence", what="OCR token")
        out.append((str(text), [float(v) for v in bbox], float(conf)))
    return out


_RECON_MODULES = (
    "copilot.documents.reconcile",
    "copilot.documents.reconciliation",
    "copilot.documents.extraction",
    "copilot.documents.pipeline",
    "copilot.documents",
)


def resolve_reconcile() -> Any:
    mods = require_any_module(*_RECON_MODULES, what="OCR reconciliation")
    candidates = []
    for mod in mods:
        for name in dir(mod):
            if name.startswith("_"):
                continue
            low = name.lower()
            if "reconcile" in low or "match_value" in low:
                obj = getattr(mod, name)
                if callable(obj) and not inspect.isclass(obj):
                    candidates.append(obj)
    if not candidates:
        pytest.fail(
            "no reconcile* callable found in copilot.documents.* — F3 reconciliation not implemented"
        )
    # Prefer a callable that takes OCR tokens explicitly.
    for fn in candidates:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if any("token" in p or "ocr" in p for p in params):
            return fn
    return candidates[0]


async def reconcile_one(fn: Any, value: str, tokens: list[dict[str, Any]], tmp_path: Any) -> Any:
    try:
        params = {p.lower() for p in inspect.signature(fn).parameters}
    except (TypeError, ValueError):
        params = set()
    if any("fact" in p for p in params) and not any("value" in p for p in params):
        semantic: list[tuple[tuple[str, ...], Any]] = [
            (("facts", "fact"), [{"field_path": "acceptance", "value": value}]),
            (("token", "tokens", "ocr", "words"), tokens),
            (("page_no", "page"), 1),
        ]
    else:
        semantic = [
            (("value", "text", "needle", "quote"), value),
            (("token", "tokens", "ocr", "words"), tokens),
            (("page_no", "page"), 1),
        ]
    out = await call_flex(fn, semantic, tmp_path=tmp_path, what="reconcile")
    if isinstance(out, (list, tuple)) and len(out) == 1:
        out = out[0]
    return out


def rects_intersect(a: list[float], b: list[float]) -> bool:
    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah
