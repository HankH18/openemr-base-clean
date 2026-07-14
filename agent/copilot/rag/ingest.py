"""Guideline-corpus ingest — discovery, chunking, idempotent persistence.

The in-repo corpus lives at ``agent/corpus/`` as Markdown files, each carrying
a ``---``-fenced front-matter block with per-source provenance metadata
(``title`` / ``source`` / ``license`` — see ``agent/corpus/LICENSES.md``).
:func:`ingest_corpus` chunks each document by ``##`` section (long sections
split on paragraph boundaries), embeds every chunk through the injected
:class:`~copilot.rag.embeddings.Embedder` (the deterministic keyless Stub in
tests/CI), and persists rows through the F1 ``MemoryRepository`` guideline
accessors.

Idempotent by design: the front-matter ``source`` is the natural key — a
document whose ``source`` is already registered is skipped wholesale, so
re-running the ingest never duplicates documents or chunks, and stub
embeddings make a from-scratch re-ingest byte-identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from copilot.memory.repository import MemoryRepository
from copilot.rag.embeddings import Embedder

#: Default in-repo corpus location: ``agent/corpus/``.
CORPUS_DIR = Path(__file__).resolve().parents[2] / "corpus"

#: Sections longer than this are split on paragraph boundaries into multiple
#: chunks (characters, not tokens — deterministic and fully offline).
MAX_CHUNK_CHARS = 1200

_REQUIRED_KEYS = ("title", "source", "license")

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


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


@dataclass(frozen=True)
class DocumentResult:
    """Outcome of ingesting (or skipping) one corpus document."""

    title: str
    source: str
    skipped: bool
    chunk_count: int


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


def chunk_body(body: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[CorpusChunk]:
    """Deterministically chunk a Markdown body by ``##`` section.

    Each ``##`` heading starts a new section (slugified into the chunk's
    ``section`` label); text before the first heading becomes a ``preamble``
    section. Sections longer than ``max_chars`` are split greedily on
    blank-line paragraph boundaries so no chunk mid-sentence-splits.
    """
    chunks: list[CorpusChunk] = []
    heading = "preamble"
    buffer: list[str] = []
    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match is None:
            buffer.append(line)
            continue
        chunks.extend(_section_chunks(heading, buffer, max_chars))
        heading = _slugify(match.group(1))
        buffer = []
    chunks.extend(_section_chunks(heading, buffer, max_chars))
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
) -> IngestReport:
    """Chunk, embed, and persist the corpus; already-ingested sources are skipped.

    Rows are written through the F1 ``MemoryRepository`` guideline accessors.
    Embeddings are computed once per document (one batched ``embed`` call) and
    persisted on each ``guideline_chunk`` row, so a later run never re-embeds
    existing content. The caller owns the transaction (commit/rollback).
    """
    repository = MemoryRepository(session)
    results: list[DocumentResult] = []
    for document in discover_corpus(corpus_dir):
        if await _document_exists(session, document.source):
            results.append(
                DocumentResult(
                    title=document.title,
                    source=document.source,
                    skipped=True,
                    chunk_count=0,
                )
            )
            continue
        vectors = embedder.embed([chunk.content for chunk in document.chunks])
        row = await repository.create_guideline_document(
            title=document.title,
            source=document.source,
            license=document.license,
        )
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
            )
        )
    return IngestReport(results=tuple(results))


async def _document_exists(session: AsyncSession, source: str) -> bool:
    """Idempotency probe (read-only), routed through the repository gateway."""
    return await MemoryRepository(session).get_guideline_document_by_source(source) is not None


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def _section_chunks(heading: str, lines: list[str], max_chars: int) -> list[CorpusChunk]:
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
            current = paragraph
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [CorpusChunk(section=heading, content=piece) for piece in pieces]
