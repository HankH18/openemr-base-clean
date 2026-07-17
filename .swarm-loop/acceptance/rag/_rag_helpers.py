"""Shared helpers for the frozen feat_rag acceptance suite.

FROZEN GOAL HARNESS — do not edit to make a test pass.

The pinned public surface these tests target (from W2_ARCHITECTURE.md):

- ``copilot.rag.embeddings.build_embedder(settings)`` — Voyage Stub/Real behind
  a Protocol; empty ``voyage_api_key`` selects the Stub. Method:
  ``embed(list[str]) -> list[list[float]]`` (1024-dim; sync or async).
- ``copilot.rag.rerank.build_reranker(settings)`` — Cohere Stub/Real behind a
  Protocol; empty ``cohere_api_key`` selects the Stub. Method:
  ``rerank(query, documents) -> reordered documents``.
- ``copilot.rag.retriever.build_retriever(settings, *, embedder=None,
  reranker=None)`` — hybrid sparse+dense retriever with RRF fusion; the
  keyword injection points let the harness substitute recording doubles.
  Method: ``retrieve(query, top_k=N)`` (sync or async).
- ``copilot.rag.retriever.rrf_fuse(sparse_ids, dense_ids)`` — reciprocal-rank
  fusion of two rankings (also accepted at ``copilot.rag``/``copilot.rag.fusion``).
- ``deidentify(text) -> str`` — the single PHI-scrub choke point (accepted at
  ``copilot.rag.deidentify``, ``copilot.rag``, or ``copilot.security``).

Defensive-import rule: a missing feature module/attr becomes ``pytest.fail``
inside the test body (ran-and-failed), never a collection error.
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import os
from collections.abc import Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

ACC_DIR = Path(__file__).resolve().parents[1]
AGENT_DIR = ACC_DIR.parents[1] / "agent"

EMBED_DIM = 1024  # voyage-3.5 — mirrors copilot.memory.db.EMBEDDING_DIM


def fail(msg: str) -> None:
    pytest.fail(msg, pytrace=False)


# --- defensive imports (missing feature => ran-and-failed, never a crash) ----


def feature_module(*names: str):
    """Import the first importable module from ``names`` or pytest.fail."""
    errors = []
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 — any import failure = feature absent
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    fail("feat_rag surface missing — none of the expected modules import:\n  " + "\n  ".join(errors))


def feature_attr(module_names: tuple[str, ...], attr_names: tuple[str, ...], what: str):
    """Resolve the first present attr across candidate modules or pytest.fail."""
    for mod_name in module_names:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001
            continue
        for attr in attr_names:
            obj = getattr(mod, attr, None)
            if obj is not None:
                return obj
    fail(f"feat_rag: {what} not found — looked for {list(attr_names)} in {list(module_names)}")


async def maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def make_settings():
    from copilot.config import Settings

    return Settings()


# --- DB access ----------------------------------------------------------------


def sync_db_url() -> str:
    url = os.environ.get("COPILOT_DATABASE_URL", "")
    assert url.startswith("sqlite+aiosqlite:///"), f"unexpected test DB url: {url!r}"
    return url.replace("sqlite+aiosqlite", "sqlite", 1)


# --- deterministic vectors + recording doubles ---------------------------------


def det_vector(text: str, dim: int = EMBED_DIM) -> list[float]:
    """Deterministic pseudo-embedding — stable per text, distinct across texts."""
    out: list[float] = []
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    counter = 0
    while len(out) < dim:
        h = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for i in range(0, 32, 4):
            out.append(int.from_bytes(h[i : i + 4], "big") / 2**32 - 0.5)
            if len(out) == dim:
                break
        counter += 1
    return out


class RecordingEmbedder:
    """Embedder double: deterministic vectors + records every outbound text."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def _record(self, texts) -> list[str]:
        items = [texts] if isinstance(texts, str) else [str(t) for t in texts]
        self.calls.append(items)
        return items

    def embed(self, texts):
        return [det_vector(t) for t in self._record(texts)]

    # Generous aliases so any reasonable Protocol method name is captured.
    def embed_texts(self, texts):
        return self.embed(texts)

    def embed_documents(self, texts):
        return self.embed(texts)

    def embed_query(self, text):
        return self.embed([text])[0]

    @property
    def captured_texts(self) -> list[str]:
        return [t for call in self.calls for t in call]


class RecordingReranker:
    """Reranker double: identity ordering + records every outbound payload."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def rerank(self, *args, **kwargs):
        query = kwargs.get("query")
        documents = kwargs.get("documents") or kwargs.get("candidates") or kwargs.get("docs")
        positional = list(args)
        if query is None and positional:
            query = positional.pop(0)
        if documents is None and positional:
            documents = positional.pop(0)
        documents = list(documents or [])
        self.calls.append({"query": query, "documents": documents})
        return documents  # identity order — preserves the fused ranking

    def __call__(self, *args, **kwargs):
        return self.rerank(*args, **kwargs)

    @property
    def captured_texts(self) -> list[str]:
        out: list[str] = []
        for call in self.calls:
            if call["query"] is not None:
                out.append(str(call["query"]))
            out.extend(str(d) for d in call["documents"])
        return out


class BoomReranker:
    """Reranker double that always fails — exercises the fused-order fallback."""

    def rerank(self, *args, **kwargs):
        raise RuntimeError("acceptance: simulated reranker outage")

    def __call__(self, *args, **kwargs):
        return self.rerank(*args, **kwargs)


# --- factory resolution ---------------------------------------------------------


def build_embedder(settings=None):
    fn = feature_attr(
        ("copilot.rag.embeddings", "copilot.rag.embedding", "copilot.rag"),
        ("build_embedder", "build_embeddings"),
        "build_embedder factory (Voyage Stub/Real behind a Protocol)",
    )
    try:
        return fn(settings or make_settings())
    except TypeError as exc:
        fail(f"pinned surface is build_embedder(settings): {exc}")


def build_reranker(settings=None):
    fn = feature_attr(
        ("copilot.rag.rerank", "copilot.rag.reranker", "copilot.rag"),
        ("build_reranker",),
        "build_reranker factory (Cohere Stub/Real behind a Protocol)",
    )
    try:
        return fn(settings or make_settings())
    except TypeError as exc:
        fail(f"pinned surface is build_reranker(settings): {exc}")


def build_retriever(settings=None, **kwargs):
    fn = feature_attr(
        ("copilot.rag.retriever", "copilot.rag"),
        ("build_retriever",),
        "build_retriever factory (hybrid FTS+dense retriever with RRF fusion)",
    )
    try:
        return fn(settings or make_settings(), **kwargs)
    except TypeError as exc:
        fail(
            "pinned surface is build_retriever(settings, *, embedder=None, reranker=None) "
            f"(injection points for the harness's recording doubles): {exc}"
        )


def resolve_deidentify():
    return feature_attr(
        ("copilot.rag.deidentify", "copilot.rag", "copilot.security.deidentify", "copilot.security"),
        ("deidentify",),
        "the deidentify() PHI-scrub choke point",
    )


# --- method-call adapters --------------------------------------------------------


async def embed_texts(embedder, texts: list[str]) -> list[list[float]]:
    for name in ("embed", "embed_texts", "embed_documents"):
        fn = getattr(embedder, name, None)
        if callable(fn):
            vecs = await maybe_await(fn(list(texts)))
            return [[float(x) for x in v] for v in vecs]
    fail("embedder must expose embed(list[str]) -> list[list[float]] (pinned Protocol surface)")


async def rerank_docs(reranker, query: str, documents: list[str]):
    fn = getattr(reranker, "rerank", None) or (reranker if callable(reranker) else None)
    if fn is None:
        fail("reranker must expose rerank(query, documents) (pinned Protocol surface)")
    try:
        result = fn(query, list(documents))
    except TypeError:
        result = fn(query=query, documents=list(documents))
    result = await maybe_await(result)
    return normalize_reranked(result, documents)


def normalize_reranked(result, documents: list[str]) -> list[str]:
    """Normalize a rerank result to an ordered list of document texts."""
    out: list[str] = []
    for item in list(result):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, int) and 0 <= item < len(documents):
            out.append(documents[item])
        elif isinstance(item, (tuple, list)) and item:
            first = item[0]
            out.append(documents[first] if isinstance(first, int) else str(first))
        else:
            for attr in ("text", "content", "document"):
                v = getattr(item, attr, None) or (
                    item.get(attr) if isinstance(item, Mapping) else None
                )
                if isinstance(v, str):
                    out.append(v)
                    break
            else:
                idx = getattr(item, "index", None)
                if isinstance(idx, int) and 0 <= idx < len(documents):
                    out.append(documents[idx])
                else:
                    fail(f"cannot normalize rerank result item {item!r}")
    return out


async def retrieve(retriever, query: str, top_k: int = 4):
    fn = None
    for name in ("retrieve", "search", "query"):
        cand = getattr(retriever, name, None)
        if callable(cand):
            fn = cand
            break
    if fn is None:
        fail("retriever must expose retrieve(query, top_k=N) (pinned surface)")
    try:
        result = fn(query, top_k=top_k)
    except TypeError:
        result = fn(query)
    return await maybe_await(result)


def evidence_items(result) -> list:
    """Normalize a retrieval result to its list of evidence items."""
    if result is None:
        fail("retriever returned None — expected a typed evidence result")
    if isinstance(result, (list, tuple)):
        return list(result)
    for attr in ("chunks", "evidence", "items", "results", "snippets", "hits"):
        v = getattr(result, attr, None)
        if isinstance(v, (list, tuple)):
            return list(v)
    fail(
        "cannot find the evidence list on the retrieval result — expected a list or an "
        f"object exposing chunks/evidence/items/results; got {type(result).__name__}"
    )


def item_get(item, *names):
    """Read the first present field from an evidence item (attr, key, or citation)."""
    holders = [item]
    for cit_name in ("citation", "source_ref"):
        cit = getattr(item, cit_name, None)
        if cit is None and isinstance(item, Mapping):
            cit = item.get(cit_name)
        if cit is not None:
            holders.append(cit)
    for holder in holders:
        for n in names:
            if isinstance(holder, Mapping) and holder.get(n) is not None:
                return holder[n]
            v = getattr(holder, n, None)
            if v is not None:
                return v
    return None


# --- fixture corpus ----------------------------------------------------------------

CORPUS = [
    (
        "Diabetic ketoacidosis (DKA) — inpatient management",
        "CC-BY-4.0",
        "acceptance-fixture:dka",
        [
            (
                "insulin-therapy",
                "In diabetic ketoacidosis, begin a continuous intravenous insulin infusion "
                "and monitor serum potassium closely during treatment.",
            ),
            (
                "fluids",
                "Initial fluid resuscitation in diabetic ketoacidosis uses isotonic saline; "
                "add dextrose once glucose falls below 200 mg/dL.",
            ),
        ],
    ),
    (
        "Sepsis — early management bundle",
        "CC-BY-4.0",
        "acceptance-fixture:sepsis",
        [
            (
                "lactate",
                "In sepsis, remeasure lactate when the initial lactate is elevated and start "
                "broad-spectrum antibiotics within one hour.",
            ),
        ],
    ),
    (
        "Anticoagulation — warfarin reversal",
        "CC-BY-4.0",
        "acceptance-fixture:anticoag",
        [
            (
                "warfarin-reversal",
                "For major bleeding on warfarin, give four-factor prothrombin complex "
                "concentrate and intravenous vitamin K.",
            ),
        ],
    ),
]


def seed_corpus(embed_one) -> dict[str, dict[str, str]]:
    """Insert the fixture corpus directly; returns {chunk_id: {section, content}}."""
    from copilot.memory.models import GuidelineChunkRow, GuidelineDocumentRow

    engine = sa.create_engine(sync_db_url())
    ids: dict[str, dict[str, str]] = {}
    try:
        with Session(engine) as session:
            for title, license_, source, chunks in CORPUS:
                doc = GuidelineDocumentRow(title=title, license=license_, source=source)
                session.add(doc)
                session.flush()
                for i, (section, content) in enumerate(chunks):
                    row = GuidelineChunkRow(
                        guideline_document_id=doc.id,
                        section=section,
                        chunk_index=i,
                        content=content,
                        embedding=embed_one(content),
                    )
                    session.add(row)
                    session.flush()
                    ids[str(row.id)] = {"section": section, "content": content}
            session.commit()
    finally:
        engine.dispose()
    return ids


# --- planted PHI (criterion 5) --------------------------------------------------------

PHI_NAME = "Marisol Quintanilla"
PHI_MRN = "99887766"
PHI_SSN = "123-45-6789"
PHI_DOB = "03/14/1962"
PHI_PHONE = "(555) 014-2977"
PHI_QUERY = (
    f"Patient: {PHI_NAME}, MRN {PHI_MRN}, DOB {PHI_DOB}, SSN {PHI_SSN}, phone {PHI_PHONE} — "
    "what do guidelines recommend for insulin therapy in diabetic ketoacidosis?"
)
# Contiguous digit runs that must never appear in any outbound payload.
PHI_DIGIT_RUNS = ("99887766", "123456789", "03141962", "5550142977")
PHI_TEXT_MARKERS = ("marisol", "quintanilla", "99887766", "123-45-6789", "03/14/1962")


def assert_no_phi(text: str, where: str) -> None:
    low = text.lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    for marker in PHI_TEXT_MARKERS:
        assert marker not in low, f"planted identifier {marker!r} leaked into {where}: {text!r}"
    for run in PHI_DIGIT_RUNS:
        assert run not in digits, f"planted identifier digits {run!r} leaked into {where}: {text!r}"
