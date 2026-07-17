"""Guideline-corpus ingest — discovery, chunking, idempotent persistence.

The in-repo corpus lives at ``agent/corpus/`` as Markdown files, each carrying
a ``---``-fenced front-matter block with per-source provenance metadata
(``title`` / ``source`` / ``license`` — see ``agent/corpus/LICENSES.md``).
:func:`ingest_corpus` chunks each document by its Markdown heading structure
(headings ``#`` to ``######``; a nested heading yields a ``parent/child`` section
breadcrumb, so a chunk carries the *path* to its section rather than just the
leaf), splits any over-length section on paragraph boundaries with a small
carried-over overlap so context is never severed mid-boundary, embeds every
chunk through the injected :class:`~copilot.rag.embeddings.Embedder` (the
deterministic keyless Stub in tests/CI), and persists rows through the F1
``MemoryRepository`` guideline accessors.

Idempotent by *content*, not merely by name. The front-matter ``source`` is the
natural key — it says *which* document a file is, never *which version*. So the
skip decision is keyed on a sha256 of the material actually persisted (title,
license, and every chunk's section/content), recorded on
``guideline_document.content_hash``: an unchanged file is skipped without
re-embedding, and an **edited** file is rebuilt automatically. Stub embeddings
make a from-scratch re-ingest byte-identical.

Why the hash is not optional bookkeeping: skipping on ``source`` alone meant a
corrected guideline silently did not apply. Fix a wrong dose in a corpus file,
re-run the ingest, and it reported ``skipped (already ingested)`` — which reads
as success — while retrieval kept serving the superseded text. And because the
serve-time verifier (``copilot.verification.serve``) re-materializes the quoted
chunk from that same stale row, the stale quote matched itself verbatim and was
served as **grounded**. The staleness was self-consistent, so the verification
gate structurally could not catch it. Comparing against the file is what breaks
that loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from copilot.memory.models import GuidelineDocumentRow
from copilot.memory.repository import MemoryRepository
from copilot.rag.embeddings import Embedder

#: Default in-repo corpus location: ``agent/corpus/``.
CORPUS_DIR = Path(__file__).resolve().parents[2] / "corpus"

#: Sections longer than this are split on paragraph boundaries into multiple
#: chunks (characters, not tokens — deterministic and fully offline).
MAX_CHUNK_CHARS = 1200

#: When an over-length section is split, this many characters of the previous
#: piece's tail are carried into the next so a query term that straddles a chunk
#: boundary still co-occurs in one chunk. Bounded and word-aligned, so the
#: overlap never grows a chunk unboundedly or begins mid-word.
CHUNK_OVERLAP_CHARS = 200

_REQUIRED_KEYS = ("title", "source", "license")

#: Any ATX heading, levels 1 to 6. Group 1 is the ``#`` run (its length = level),
#: group 2 the heading text. Setext (``===``/``---``) headings are not used by
#: the corpus and are intentionally out of scope.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class CorpusChunk:
    """One retrievable unit of a corpus document."""

    section: str
    content: str


@dataclass(frozen=True)
class CorpusDocument:
    """A parsed corpus source file, ready to persist."""

    path: Path
    title: str
    source: str
    license: str
    chunks: tuple[CorpusChunk, ...]

    @property
    def content_hash(self) -> str:
        """sha256 of everything this document persists, for change detection.

        Derived rather than stored, so it cannot drift from the content it
        describes. Covers the *material* fields — ``title``, ``license``, and each
        chunk's ``section``/``content`` — i.e. exactly the values written to
        ``guideline_document`` / ``guideline_chunk``. Hashing the derived chunks
        rather than the raw file body is deliberate: it also moves when the
        *chunker* changes (new ``MAX_CHUNK_CHARS``, heading handling), which
        likewise makes the stored rows wrong, and it ignores edits that change no
        persisted value (a comment in the front matter, trailing whitespace).

        ``source`` is excluded: it is the lookup key this hash is compared *under*,
        so it is identical on both sides of every comparison by construction.

        ``path`` is excluded too — moving a corpus file without editing it changes
        nothing that is served, and re-embedding the corpus over a rename would be
        pure cost.

        Canonical JSON (sorted keys, fixed separators) so the digest depends on
        values only, never on dict ordering — mirroring
        ``copilot.worker.hashing.content_hash_for_resources``.
        """
        payload = {
            "title": self.title,
            "license": self.license,
            "chunks": [
                {"section": chunk.section, "content": chunk.content} for chunk in self.chunks
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


#: Why a document was (re-)ingested or skipped. ``unchanged`` is the only value
#: that skips; every other value rebuilds the document.
IngestReason = Literal["new", "changed", "unknown-hash", "forced", "unchanged"]

#: Operator-facing wording for each reason — the ingest report is the only signal
#: an operator gets, and "skipped" alone is what made the staleness bug read as
#: success. Each line now says what the ingester actually concluded.
REASON_LABELS: dict[IngestReason, str] = {
    "new": "ingested (new)",
    "changed": "re-ingested (content changed)",
    "unknown-hash": "re-ingested (no recorded hash — pre-0009 row, freshness unknown)",
    "forced": "re-ingested (--force)",
    "unchanged": "skipped (unchanged)",
}


@dataclass(frozen=True)
class DocumentResult:
    """Outcome of ingesting (or skipping) one corpus document."""

    title: str
    source: str
    skipped: bool
    chunk_count: int
    reason: IngestReason = "unchanged"

    @property
    def label(self) -> str:
        """Operator-facing description of this outcome."""
        return REASON_LABELS[self.reason]


@dataclass(frozen=True)
class IngestReport:
    """Aggregate outcome of one ingest run."""

    results: tuple[DocumentResult, ...]

    @property
    def documents_ingested(self) -> int:
        return sum(1 for result in self.results if not result.skipped)

    @property
    def documents_skipped(self) -> int:
        return sum(1 for result in self.results if result.skipped)

    @property
    def chunks_ingested(self) -> int:
        return sum(result.chunk_count for result in self.results if not result.skipped)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a ``---``-fenced ``key: value`` front-matter block from the body.

    Returns ``({}, text)`` unchanged when the file has no front matter (such
    files are not corpus sources). Raises :class:`ValueError` on a malformed
    or unterminated block — corpus files are in-repo, so fail loudly.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        stripped = line.strip()
        if stripped == "---":
            return meta, "\n".join(lines[index + 1 :])
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            raise ValueError(f"malformed front-matter line (expected 'key: value'): {line!r}")
        meta[key.strip().lower()] = value.strip()
    raise ValueError("unterminated front-matter block (missing closing '---')")


def chunk_body(
    body: str,
    *,
    max_chars: int = MAX_CHUNK_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[CorpusChunk]:
    """Deterministically chunk a Markdown body along its heading structure.

    Each ATX heading (``#`` to ``######``) starts a new section. Headings nest by
    level, so a chunk's ``section`` label is the slugified breadcrumb of the
    heading path (``fluid-therapy/rates`` for a ``###`` under a ``##``); a lone
    top-level heading is just its own slug, so a flat ``##``-only document is
    unchanged. Text before the first heading becomes a ``preamble`` section.

    Sections longer than ``max_chars`` are split greedily on blank-line
    paragraph boundaries so no chunk mid-sentence-splits, and each split piece
    after the first is prefixed with a bounded, word-aligned ``overlap_chars``
    tail of the previous piece so context spanning the boundary is retained.
    """
    chunks: list[CorpusChunk] = []
    breadcrumb: list[tuple[int, str]] = []
    section = "preamble"
    buffer: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue
        chunks.extend(_section_chunks(section, buffer, max_chars, overlap_chars))
        level = len(match.group(1))
        slug = _slugify(match.group(2))
        while breadcrumb and breadcrumb[-1][0] >= level:
            breadcrumb.pop()
        breadcrumb.append((level, slug))
        section = "/".join(part for _level, part in breadcrumb)
        buffer = []
    chunks.extend(_section_chunks(section, buffer, max_chars, overlap_chars))
    return chunks


def discover_corpus(corpus_dir: Path | None = None) -> list[CorpusDocument]:
    """Parse every corpus source under ``corpus_dir`` (default ``agent/corpus/``).

    Discovery is deterministic (sorted filenames). A ``*.md`` file without a
    front-matter block (e.g. ``LICENSES.md``) is not a source and is skipped;
    a source missing any of the required ``title``/``source``/``license``
    keys is a hard error — license metadata is mandatory per source.
    """
    root = corpus_dir if corpus_dir is not None else CORPUS_DIR
    if not root.is_dir():
        raise FileNotFoundError(f"guideline corpus directory not found: {root}")
    documents: list[CorpusDocument] = []
    for path in sorted(root.glob("*.md")):
        try:
            meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise ValueError(f"invalid corpus front matter in {path}") from exc
        if not meta:
            continue
        missing = [key for key in _REQUIRED_KEYS if not meta.get(key, "").strip()]
        if missing:
            raise ValueError(f"{path}: front matter is missing required key(s): {missing}")
        chunks = chunk_body(body)
        if not chunks:
            raise ValueError(f"{path}: no ingestible content below the front matter")
        documents.append(
            CorpusDocument(
                path=path,
                title=meta["title"],
                source=meta["source"],
                license=meta["license"],
                chunks=tuple(chunks),
            )
        )
    if not documents:
        raise ValueError(f"no corpus sources (front-mattered *.md files) found under {root}")
    return documents


async def ingest_corpus(
    session: AsyncSession,
    embedder: Embedder,
    *,
    corpus_dir: Path | None = None,
    force: bool = False,
) -> IngestReport:
    """Chunk, embed, and persist the corpus; only *unchanged* sources are skipped.

    Rows are written through the F1 ``MemoryRepository`` guideline accessors.
    Embeddings are computed once per document (one batched ``embed`` call) and
    persisted on each ``guideline_chunk`` row. The caller owns the transaction
    (commit/rollback).

    The skip decision compares the file's :attr:`CorpusDocument.content_hash` with
    the hash recorded on the stored row, so re-running the ingest after editing a
    corpus file **applies the edit**. This is the correct default rather than an
    opt-in flag: a clinical guideline that has been corrected must not keep being
    served, and the previous name-only check made that failure both silent and
    *unverifiable* — the serve-time verifier re-reads the same stale row, so the
    stale quote matched itself and was served as grounded. An operator cannot be
    expected to remember a flag to avoid citing a retracted dose.

    Costs nothing when nothing changed: the hash is compared *before* any
    ``embed`` call, so an unchanged corpus still performs zero embedding work and
    zero writes.

    Three states rebuild a document:

    * **new** — no row for this ``source`` yet.
    * **changed** — recorded hash differs from the file's.
    * **unknown-hash** — the row predates migration 0009 and carries ``NULL``. That
      is *unknown*, not *unchanged*: nothing on the row can establish whether it
      matches the file. Rebuilding once is the only honest reading — treating it as
      current would preserve the exact staleness bug for every corpus already
      deployed, which is the population most likely to hold the stale text. It is a
      one-time cost per document: the rebuild records a hash, and subsequent runs
      take the cheap unchanged path.

    ``force`` rebuilds unconditionally, hash or no hash. Still required when the
    *embedder* changes: vectors written by an old embedder are incomparable with
    queries embedded by a new one, and that degradation lives in the embedding, not
    in the corpus text — the content hash cannot see it. Safe by design — the corpus
    is reproducible from the repo (see ``discover_corpus``), so rebuilding these rows
    destroys nothing irreplaceable.
    """
    repository = MemoryRepository(session)
    results: list[DocumentResult] = []
    for document in discover_corpus(corpus_dir):
        existing = await repository.get_guideline_document_by_source(document.source)
        reason = _ingest_reason(existing, document, force=force)
        if reason == "unchanged":
            results.append(
                DocumentResult(
                    title=document.title,
                    source=document.source,
                    skipped=True,
                    chunk_count=0,
                    reason=reason,
                )
            )
            continue
        if existing is not None:
            # Replace, never accumulate: a second row for one source would
            # double-count in retrieval and let the stale chunks stay reachable.
            await repository.delete_guideline_document_by_source(document.source)
        vectors = embedder.embed([chunk.content for chunk in document.chunks])
        row = await repository.create_guideline_document(
            title=document.title,
            source=document.source,
            license=document.license,
        )
        # Recorded in the same unit of work as the chunks it describes: a hash
        # committed without its chunks (or vice versa) would claim a freshness the
        # rows do not have, and the next run would trust it and skip.
        row.content_hash = document.content_hash
        for index, (chunk, vector) in enumerate(zip(document.chunks, vectors, strict=True)):
            await repository.create_guideline_chunk(
                guideline_document_id=row.id,
                content=chunk.content,
                section=chunk.section,
                chunk_index=index,
                embedding=vector,
            )
        results.append(
            DocumentResult(
                title=document.title,
                source=document.source,
                skipped=False,
                chunk_count=len(document.chunks),
                reason=reason,
            )
        )
    return IngestReport(results=tuple(results))


def _ingest_reason(
    existing: GuidelineDocumentRow | None, document: CorpusDocument, *, force: bool
) -> IngestReason:
    """Classify one document's outcome — the whole skip/rebuild decision, in one place.

    Ordered most-decisive first. ``force`` wins over every state (it exists to
    rebuild rows the hash cannot judge, e.g. after an embedder change), and an
    absent or unhashed row is rebuilt before any hash comparison is attempted —
    comparing against ``None`` would silently be a mismatch and get the right
    answer for the wrong reason.
    """
    if force:
        return "forced"
    if existing is None:
        return "new"
    if existing.content_hash is None:
        return "unknown-hash"
    if existing.content_hash != document.content_hash:
        return "changed"
    return "unchanged"


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def _section_chunks(
    section: str, lines: list[str], max_chars: int, overlap_chars: int
) -> list[CorpusChunk]:
    text = "\n".join(lines).strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    pieces: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if current and len(candidate) > max_chars:
            pieces.append(current)
            tail = _overlap_tail(current, overlap_chars)
            current = f"{tail}\n\n{paragraph}" if tail else paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [CorpusChunk(section=section, content=piece) for piece in pieces]


def _overlap_tail(text: str, overlap_chars: int) -> str:
    """The last ``overlap_chars`` characters of ``text``, aligned to a word start.

    Deterministic and bounded: never longer than ``overlap_chars`` and trimmed
    forward to the first whitespace so the carried-over context never begins in
    the middle of a word. Returns ``""`` when overlap is disabled.
    """
    if overlap_chars <= 0:
        return ""
    if len(text) <= overlap_chars:
        return text
    tail = text[-overlap_chars:]
    parts = re.split(r"\s+", tail, maxsplit=1)
    remainder = parts[1] if len(parts) == 2 else tail
    return remainder or tail
