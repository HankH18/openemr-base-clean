# W2_ARCHITECTURE — Clinical Co-Pilot, Week 2

> **What this is:** the Week 2 architecture document — the document ingestion flow, worker
> graph, RAG design, eval gate, risks, and tradeoffs. Written to be concrete and unambiguous
> so it also serves as the build spec. The design was decided via a checkpoint-gated ideation
> pass; the decisions log lives at `agent/research/week2/04-technical-decisions.md` and the
> matching diagram at `agent/research/week2/03-architecture.mmd`.
>
> **Baseline:** builds on the accepted Week 1 Clinical Co-Pilot (a hospitalist rounding
> co-pilot). Week 2 adds multimodal document ingestion, a supervisor + worker graph, and
> hybrid RAG — all behind the existing fail-closed verification and eval discipline.

## Overview

Week 2 turns the co-pilot into a **multimodal evidence agent**. When an outside/transfer
document lands in an admitted patient's chart — a scanned **lab PDF** or an **intake form** —
the co-pilot *sees* it, extracts strict-schema facts with **pixel-level provenance**, tells the
hospitalist what's new or conflicting vs. the existing chart, backs the answer with **guideline
evidence kept visibly separate from patient facts**, and makes **every clinical claim clickable
to its exact source** (a bounding box on the scanned page, a FHIR record, or a guideline chunk).
Work is routed by a **hand-rolled supervisor** across two workers (intake-extractor,
evidence-retriever) plus a critic, with logged handoffs and nested traces. Quality is defended by
a **two-tier eval gate**: a deterministic, CI-blocking suite of 53 boolean-rubric cases that fails
CI on regression, and a separate non-blocking live-model quality run.

The system is intentionally **narrow**: the MVP shipped exactly two document types, one small
guideline corpus, one supervisor + two workers, one regression gate. **Post-MVP (Early Submission)
additions, all behind the same frozen 12/12 harness:** a third document type (`medication_list`);
contextual-retrieval upgrades (clinical-abbreviation query expansion after the de-identify choke
point, heading-aware chunking, section-match boost); a write-back auto-propose bridge (categorized
intake facts → `ProposedWrite` candidates through the propose→confirm gate, agent never
self-commits); and a genuinely keyed `RealCritic` (an LLM consistency/safety pass that can only
demote cited claims, never loosen the citation gate). Still deliberately out of scope: visual
multi-vector (ColQwen2) indexing and a MinIO/Grafana scale-out (documented scale paths).

## Design principles & constraints

1. **The schema is the source of truth.** Raw VLM output never bypasses Pydantic validation; a
   field that doesn't validate is rejected, not coerced.
2. **Fail-closed grounding is preserved end-to-end.** Every clinical claim must re-materialize its
   cited source *and* pass a value re-check, or it is dropped. This is the Week 1 invariant and it
   must not regress.
3. **Extraction without invention.** An extracted value is "supported" only if it can be located
   on the page (OCR corroboration); unmatched values are flagged, never silently trusted.
4. **Deterministic core, AI edges.** The trust boundary is deterministic code (verifier, schema
   validation, PHI scrub). LLMs extract, retrieve, converse, and critique; they never *are* the
   safety control.
5. **Build with the grain.** Mirror the existing Stub/Real-behind-a-`Protocol` + `build_*` factory
   pattern so the keyless test suite stays green. New multi-agent code lives in a **new package**
   (`copilot/graph/`) — `copilot/worker/` already means the background poller.
6. **One source of truth per data type; no silent overwrites.** OpenEMR owns source documents and
   physician-confirmed clinical records; the agent DB owns derived extractions, citations, the
   guideline corpus, and audit. Derived-fact storage is **append-only** (re-ingest = new version).
7. **Minimize PHI egress.** Document images + PHI go only to Claude (as in Week 1). Voyage and
   Cohere receive only **scrubbed clinical-topic queries** — enforced at a single choke-point shared
   with logging. The scrub is shape-based (structured identifiers + label-gated names) and has a
   documented free-text-name gap — see §Security for the precise limitation; "de-identified" here
   means *this scrub ran*, not Safe Harbor.
8. **Correlation-ID everywhere.** A full multi-agent trace must be reconstructable from the
   correlation ID alone; worker spans are children of the supervisor span.

## Components

**Supervisor** (`copilot/graph/supervisor.py`) — decides, per request, whether extraction is
needed, whether evidence retrieval is needed, and when the final answer is ready. Emits typed,
logged `Handoff` objects; opens the parent span.

> **As built.** The graph is wired into `POST /v1/chat` behind the `chat_graph_enabled` flag
> (env `COPILOT_CHAT_GRAPH_ENABLED`, **default off**): with the flag off the endpoint uses the
> inline verify path (byte-for-byte the Week-1 behavior); with it on, the turn is routed through
> the graph (supervisor → workers → critic → deterministic verifier), threading conversation
> history and the smart-mode delegated FHIR client through unchanged. The "no grounded claims →
> withheld" override is applied by the chat service in **both** modes (the graph deliberately does
> not duplicate it), so the graph does not yet *own* that decision — a documented follow-up, not a
> gap in the safety behavior, which is identical either way. A regression test
> (`agent/tests/test_chat_graph.py`) proves the flag-on path actually drives the graph (asserts the
> `graph.run`/`supervisor.route` spans + `worker.handoff` events fire) and that the flag-off default
> path does not.

**Intake-extractor worker** (`copilot/graph/intake_extractor.py`) — owns document extraction: PDF
rasterization → OCR → Claude-vision structured extraction → schema validation → OCR reconciliation
(bbox + confidence). Produces `ExtractedFact`s and their citations. Does *not* decide the final
answer.

**Evidence-retriever worker** (`copilot/graph/evidence_retriever.py`) — owns hybrid retrieval over
the guideline corpus: de-identify query → sparse (Postgres FTS) + dense (pgvector storage,
Python-side cosine over the loaded chunk set — see §RAG index) → RRF fusion → Cohere rerank →
top-K grounded snippets with `chunk_id + section`. Returns evidence, explicitly typed as guideline
(not patient) facts.

**Critic** (`copilot/graph/critic.py`) — an *additional* gate over the drafted answer: rejects any
clinical claim lacking a machine-readable citation (deterministic check) and unsafe action
suggestions (LLM judgment, kept narrow — the agent still does not recommend treatments). Augments,
never replaces, the deterministic verifier.

**Verifier (extended)** (`copilot/verification/`) — the existing fail-closed gate, generalized to
three citation source types (see Data model). Adds a **document** grounding path (re-check the
claim value against the stored, schema-validated extraction, requiring a reconciled bbox at/above a
confidence threshold) and a **guideline** path (quoted text must appear in the stored chunk).
FHIR path unchanged.

**Document service** (`copilot/documents/`) — orchestrates ingestion: store source doc in OpenEMR
via the write client, persist page renders + OCR tokens + extraction (append-only) in the agent DB,
audit each step. Exposes async status.

**Write client (extended)** (`copilot/fhir/write_client.py`) — add `upload_document(...)` (OpenEMR
Standard API `POST /api/patient/:pid/document`, multipart) and extend the writable surface to
`medical_problem` and `allergy` (already supports vitals/meds/encounters) for physician-confirmed
intake write-back via the existing propose→confirm gate.

**RAG index** (`copilot/rag/`) — corpus ingest script (repo-reproducible), chunking, Voyage
embeddings (precomputed at ingest + cached), pgvector + FTS storage, hybrid retrieve + Cohere
rerank. All behind swappable/stubbable `Protocol`s.

> **How dense retrieval actually works — stated precisely, because "pgvector" oversells it.**
> pgvector is the **storage column type only** (`Vector(1024)` on Postgres, a JSON list on SQLite —
> `copilot/memory/db.py:49`). There is **no vector operator** (`<=>`, `<->`, `cosine_distance`) and
> **no ANN index** (no `ivfflat`, no `hnsw`) in any SQL in this repo. Dense retrieval is an
> **in-Python full scan**: `MemoryRepository.list_guideline_chunks()` loads *every* chunk row
> (`copilot/memory/repository.py:710-719` — "the hybrid retriever loads the full chunk set to rank
> it in memory"), and `_dense_rank()` computes cosine similarity over each row in Python
> (`copilot/rag/retriever.py:238-250`). Only the **sparse** half touches Postgres query planning
> (`to_tsvector`/`plainto_tsquery`, `retriever.py:286-295`), with a term-overlap fallback on SQLite.
>
> This is **correct and fast at the committed corpus size** — 4 guideline documents / 19 heading
> sections (`agent/corpus/`), so the scan is ~19 vectors per query and the measured retrieval floor
> is **p95 2.0 ms** (`agent/artifacts/latency_report.json`). It is **not an indexed search**, and it
> is O(corpus) in both rows loaded and Python cosine ops per query — it will degrade linearly as the
> corpus grows.
>
> **Scale path (not built):** switch `_dense_rank` to an in-SQL `ORDER BY embedding <=> :query_vec
> LIMIT k` and add an ANN index (`ivfflat` or `hnsw`) on `guideline_chunk.embedding`. The storage
> column is already the right type, so this is a retriever-internal change behind the existing
> `GuidelineRetriever` surface — no schema migration and no contract change. The trigger is corpus
> growth (order hundreds-to-thousands of chunks), not current load.

**Observability (extended)** (`copilot/observability/`) — nested spans; wired JSON logging;
per-encounter cost across the graph; graded `/ready`; SLOs + alerts; hand-rolled status page.

**Frontend** (`agent/web/`) — React Aria upload (`FileTrigger`), extended `ProvenanceChip`
(document/fhir/guideline variants), SVG-over-image bbox overlay, reused `MetricChart`/`TrendChip`.

## Tech stack

Concrete choices **with versions**; anything pinned by the existing codebase is noted.

- **Backend:** Python 3.12 · FastAPI `>=0.115,<0.116` · Pydantic v2 (`>=2.9`) · SQLAlchemy 2
  (async) · Anthropic SDK `>=0.40,<1` (installed 0.116; **pinned `<1`**) · APScheduler · httpx ·
  cryptography · authlib · Langfuse **v2** (`>=2.55,<3` — v3's OTel API emits nothing here).
- **Datastore:** PostgreSQL **16** with **pgvector** (`pgvector/pgvector:pg16`) — used as the
  embedding **storage column type**, not as a search index; dense ranking is Python-side cosine
  (see §RAG index). SQLite (aiosqlite) in tests via a JSON-vector fallback column mirroring the
  existing `JSONType` dual-dialect trick.
- **Document pipeline (new deps):** Tesseract (system binary) + `pytesseract` · `pypdfium2`
  (rasterization; BSD/Apache — deliberately *not* PyMuPDF, which is AGPL) · `Pillow`.
- **Embeddings:** **Voyage AI** `voyage-3.5` (1024-dim) — Anthropic's recommended embedding
  partner; corpus vectors precomputed at ingest and cached (CI-deterministic).
- **Reranker:** **Cohere** `rerank-v3.5`.
- **Vision/extraction:** Claude vision-capable Sonnet (repo default id `claude-sonnet-5` — **verify
  the real model id before a live run**); tool-forced JSON for extraction (a deliberate departure
  from the current prompt-instructed-JSON convention).
- **Frontend:** React `18.3` · Vite `6` · React Aria Components `1.6` · TypeScript `5.7`;
  hand-written CSS design tokens (theme-aware). No chart/PDF library — hand-rolled SVG (house style).
- **Deploy:** single DigitalOcean droplet · `docker-compose.deploy.yml` · Caddy 2 (sole ingress).

`pytest` runs with `filterwarnings=["error"]` — **pin every new dependency and clear its warnings**,
or tests fail.

## Data model

New Alembic migration **`0005`**, chained off `0004`. Runs `CREATE EXTENSION IF NOT EXISTS vector`
on Postgres. New tables (Pydantic contracts in/out via `MemoryRepository`, `JSONType` for JSON
columns, a dialect-switching vector column for embeddings):

- **`source_document`** — `id` PK · `patient_id` · `openemr_document_id` (authoritative source
  ref) · `doc_type` enum(`lab_pdf`,`intake_form`,`medication_list`) · `category_path` · `content_hash` · `page_count`
  · `status` enum(`uploaded`,`extracting`,`extracted`,`failed`) · `correlation_id` · `created_at`.
- **`document_page`** — `id` PK · `source_document_id` FK · `page_no` · `image` (bytea, re-derivable
  cache) · `width` · `height` · `ocr_tokens` JSONB `[{text, bbox:[x,y,w,h], conf}]`.
- **`extraction`** — `id` PK · `source_document_id` FK · `schema_version` · `model` ·
  `confidence_overall` · `status` · `correlation_id` · `created_at`. **Append-only** (re-ingest =
  new row).
- **`extracted_fact`** — `id` PK · `extraction_id` FK · `field_path` · `value` · `unit?` ·
  `reference_range?` · `abnormal_flag?` · `collection_date?` · `page_no` · `bbox` JSONB ·
  `match_confidence` · `supported` bool · `category?` (`IntakeCategory` — the OpenEMR record type
  for an intake fact; NULL for lab facts).
- **`guideline_document`** — `id` PK · `title` · `source` · `license` · `ingested_at`.
- **`guideline_chunk`** — `id` PK · `guideline_document_id` FK · `section` · `chunk_index` · `text`
  · `embedding vector(1024)` · `fts tsvector` (GIN-indexed).

**Citation (no new table — evolves in `memory_file.summary` JSON + the frontend contract):** a
discriminated union on `source_type`:

```python
# copilot/domain/primitives.py — Citation is Claim.source_ref
Citation = FhirCitation | DocumentCitation | GuidelineCitation   # discriminated on source_type

class DocumentCitation(BaseModel):        # source_type = "document"
    source_type: Literal["document"]
    source_id: str        # source_document_id
    page_or_section: int  # page_no
    field_or_chunk_id: str  # extracted_fact.id / field_path
    quote_or_value: str
    bbox: list[float]     # normalized [x, y, w, h]
    confidence: float

class GuidelineCitation(BaseModel):       # source_type = "guideline"
    source_type: Literal["guideline"]
    source_id: str        # guideline_document_id
    page_or_section: str  # section
    field_or_chunk_id: str  # guideline_chunk.id
    quote_or_value: str

# FhirCitation = the Week 1 FhirReference, source_type="fhir" (default for existing claims)
```

**Migration note (Week 1 → Week 2 schema evolution):** existing persisted claims have no
`source_type`; the deserializer defaults them to `"fhir"` and maps the old `FhirReference` fields
into `FhirCitation`. All new columns are nullable/defaulted so old rows rehydrate unchanged
(the repository already uses `.get(...)` defaults). No backfill required.

### Ownership, lineage, access control, and validation — per artifact type

Principle 6 ("one source of truth per data type") is only meaningful if every artifact names its
owner. All four Week-2 artifact types, made explicit:

| Artifact | Owner / authority | Lineage (where it came from) | Access control | Validation |
|---|---|---|---|---|
| **Extracted lab observations** (`extracted_fact`, `category IS NULL`) | **Agent DB — agent-store-authoritative.** Not OpenEMR: labs/Observations are **read-only** in both the FHIR and Standard APIs (verified against the route maps), so a lab fact *cannot* be written back as a FHIR resource. OpenEMR still owns the **source PDF**. | `source_document` (→ `openemr_document_id`, the authoritative source ref) → `document_page` (raster + OCR tokens) → `extraction` (model, schema_version) → `extracted_fact` (+ `page_no`, `bbox`, `match_confidence`). **Append-only**: re-ingest = new `extraction` row, never an overwrite. | PHI. Route-gated: session auth + **rounding-list RBAC** — `is_authorized(clinician_id, patient_id)` on every route (`api/routes/documents.py:131,188,236`), which requires a persisted rounding cursor covering that patient (`auth/authorization.py:20-28`). Every read writes an `audit_log` row. | Strict Pydantic `LabReport` schema — a field that fails validation is **rejected, not coerced**. Plus OCR reconciliation: a value not locatable in the page tokens gets `supported=false` and **cannot pass the document-grounding gate** (threshold `doc_extraction_confidence_threshold`, default 0.7, `config.py:306`). |
| **Intake facts** (`extracted_fact` with a non-NULL `category`) | **Split, deliberately.** The agent DB owns the *extraction*; **OpenEMR owns the clinical record** once a physician confirms it. The agent **never self-commits** — write-back goes through the propose→confirm gate, and the confirmed row is OpenEMR's, attributed to the physician's own token. | Same chain as lab facts, plus `IntakeFact.category` (`IntakeCategory` enum) mapping each fact to its OpenEMR record type (`demographic`→`patient_data`, `medication`/`allergy`/`medical_problem`→`lists.type`, …; migration `0007`). Confirmed facts then become OpenEMR-owned records — lineage crosses the boundary **only** through the gate. | PHI. Identical to lab facts (same routes, same RBAC). Write-back additionally requires `writeback_enabled` **and** the physician's delegated write scopes; propose and commit are separately audited. | Strict Pydantic `IntakeForm` / `IntakeFact` schema + the same OCR-reconciliation gate. `IntakeCategory` is a **closed enum** — an uncategorizable fact is not silently written back. |
| **Guideline chunks** (`guideline_document`, `guideline_chunk`) | **Agent DB — agent-owned, and fully repo-reproducible.** The corpus is *derived data*, not a system of record: the committed Markdown under `agent/corpus/` is the real source of truth and `agent/scripts/ingest_guidelines.py` deterministically rebuilds every row from it. Losing the table costs a re-ingest, not data (see `DEPLOY.md` §19.2). | `agent/corpus/*.md` (committed, each with front-matter `source` + a license recorded in `guideline_document.license`) → heading-aware chunking (`rag/ingest.py`) → Voyage embedding (or the keyless stub) → `guideline_chunk.{text, embedding, fts}`. **Idempotent by CONTENT** — `guideline_document.content_hash` (sha256 over the derived chunks) decides: unchanged is skipped before any embed call, changed is re-ingested automatically, and a NULL hash (a pre-`0009` row) is treated as *unknown* and rebuilt once. It keyed on the `source` natural key alone until `0009`, which meant a **corrected guideline silently did not apply** — and because the serve-time verifier re-materializes the chunk from that same stale row, the old text then re-verified as *grounded*. Staleness was self-consistent, so the fail-closed verifier structurally could not catch it. `--force` remains, and is still genuinely required for an **embedder** change: that degradation lives in the vectors, not the text, so no content hash can see it. | **Non-PHI — public clinical text.** No patient scoping applies or is needed. There is **no public guideline endpoint**: chunks are reachable only *through* an authorized chat turn (`api/routes/chat.py:117`, same RBAC), so no unauthenticated read path exists even though the content is public. Egress: only **scrubbed** queries reach Voyage/Cohere (§Security), never patient facts. | Structural, not clinical: `title` + `source` + `license` front-matter are **required at ingest** (`_REQUIRED_KEYS`, `rag/ingest.py:45`) — a file missing any is rejected; chunk text carries `section` + `chunk_index`. Retrieval returns typed `GuidelineEvidence`, never a patient-fact `Claim`, and a chunk is served **verbatim from storage** — the retrieval path cannot invent one. **But the corpus content itself is demo material, and the docs must say so:** each file's front-matter records `provenance: Original text synthesized for the AgentForge demo corpus from general medical knowledge; not excerpted from any copyrighted guideline. Demonstration only — not for clinical use.` It is **not** a published clinical guideline and carries **no clinical authority** — it exercises the RAG path with license-clean text. A real deployment must replace `agent/corpus/` with licensed guideline sources; the ingest contract (front-matter + heading chunking) is unchanged by that swap. |
| **Citation records** (`Citation` union in `memory_file.summary` JSON) | **Agent store — authoritative.** No separate table: the discriminated union is persisted with the claim it belongs to, so a claim and its provenance can never drift apart or be independently deleted. | Emitted by whichever path produced the claim: `FhirCitation` ← a live FHIR read; `DocumentCitation` ← `extracted_fact` (carrying `source_id`, `page_no`, `bbox`, `confidence`); `GuidelineCitation` ← `guideline_chunk` (`chunk_id` + `section`). Each points back to a **re-materializable** row — that is the whole design. | Inherits the access control of the claim it accompanies: served only on routes gated by session auth + rounding-list RBAC. A `GuidelineCitation` is non-PHI; `DocumentCitation`/`FhirCitation` are PHI-bearing. | **Pydantic discriminated union on `source_type`** — an unknown type fails deserialization rather than degrading to an untyped ref. Enforced at serve time by the **fail-closed verifier**: every claim must re-materialize its cited source *and* pass a value re-check, or it is dropped; if no claims survive, the answer is withheld. Legacy rows with no `source_type` default to `"fhir"` (migration note above). |

**The invariant this table encodes:** *the agent owns what it derived; OpenEMR owns what a physician
confirmed; the repo owns what is reproducible.* No artifact has two owners, and the one place data
crosses an ownership boundary — intake fact → OpenEMR record — is exactly where the propose→confirm
gate sits.

**Durability follows ownership.** Each row's owner determines how it is protected and what losing it
costs — full backup/recovery procedure, RPO/RTO estimates, and the (honestly stated) gap that
**no scheduled backup runs today** are in [`DEPLOY.md`](DEPLOY.md) §19. The short version: the
repo-owned tier (eval golden set, rubrics, baseline, guideline corpus) is **RPO 0 — reproducible
from the repo alone**, verified by `python evals/gate.py` on a clean clone with no key, DB, or
network; the agent-owned derived extractions are the genuinely fragile tier, since OpenEMR holds the
source PDF but never our parse of it.

## Interfaces & contracts

HTTP (new/changed; all under `/v1`, auto-mounted from `copilot/api/routes/`, proxied by Caddy):

- `POST /v1/documents` — multipart (`file`, `patient_id`, `doc_type`). → `202 {document_id,
  status, correlation_id}`. Auth: delegated SMART token with `patients/docs write`; RBAC
  rounding-list gate as on other PHI routes.
- `GET /v1/documents/{document_id}` — → `{status, doc_type, page_count, extraction:{facts:[
  ExtractedFact], confidence_overall}, citations:[Citation]}`.
- `GET /v1/documents/{document_id}/pages/{n}` — page image (for the overlay backdrop).
- `POST /v1/chat` (extended) — answer claims carry the `Citation` union; guideline evidence is a
  separate, labeled block in the response, never mixed into patient-fact claims.

OpenAPI 3.0 spec committed at `agent/openapi/week2.yaml`, kept in sync; contract tests assert the
implementation matches. Bruno/Postman collection updated with document upload, extraction status,
evidence retrieval, and the full Week 2 flow.

Internal graph contracts (Pydantic, frozen): `AgentTask`, `Handoff{from_agent, to_agent, reason,
payload}` (logged), `WorkerResult`, `ExtractionResult`, `EvidenceResult`, `CriticVerdict`.

OpenEMR (existing routes, verified in this fork): source-doc upload =
`POST /api/patient/:pid/document` (multipart `document`, `path`, optional `eid`; readable back as a
FHIR `DocumentReference`); writable intake facts = `POST/PUT /api/patient/:puuid/medical_problem`,
`.../allergy`, `.../medication`. **Labs/Observations are read-only in both FHIR and the Standard
API** — confirmed against the route maps — which is *why* lab facts stay agent-store-authoritative.

## Data flow

Ingestion (async): `POST /v1/documents` → store source in OpenEMR (`upload_document`, gets
`openemr_document_id`) → rasterize pages (`pypdfium2`) → OCR (`pytesseract`, word boxes) → Claude
vision extraction into the strict schema (tool-forced JSON) → **reconcile** each value to OCR
tokens (attach bbox + match confidence; unmatched → `supported=false`, flagged) → persist
`extraction` + `extracted_fact`s (append-only) → audit. Status polled via
`GET /v1/documents/{id}`.

Answer (chat): supervisor opens the parent span → routes to intake-extractor (if a document is in
scope / referenced) and/or evidence-retriever (if guideline backing is needed) → drafts an answer
whose claims cite fhir/document/guideline sources → **verifier** re-materializes and re-checks every
claim (fail-closed) → **critic** rejects uncited/unsafe → served answer separates patient facts from
guideline evidence; each claim renders a citation chip; document claims open the page image with the
bbox highlighted. Full trace reconstructable from the correlation ID.

## Security

- **Auth/identity:** reuse the Week 1 SMART session + per-physician delegated token; document
  writes use the physician's token (`patients/docs write`) so OpenEMR attributes them natively.
  RBAC rounding-list gate on every new PHI route (the `is_authorized` pattern).
- **PHI egress:** a single `deidentify()` choke-point strips patient identifiers before any Voyage
  or Cohere call *and* before logging. Document **images** are sent only to Claude. Guideline chunks
  are public (non-PHI).
  - **Known limitation — state it plainly.** `deidentify()` (`copilot/rag/deidentify.py`) is a
    deterministic regex scrub, not a model. It removes **structured identifiers by shape** — email,
    SSN, dates, phone, and any run of 5+ digits (MRN/account) — and **label-gated names** only:
    the `_LABELED_NAME_RE` pattern (`deidentify.py:50-53`) requires an explicit `Patient:` / `Pt:` /
    `Name -` label followed by Title-Case tokens. It therefore **does not scrub an arbitrary
    free-text name**: a clinician typing *"Marisol's lactate is 4.2 — what does the sepsis guideline
    say?"* sends that name to Voyage/Cohere. This is a deliberate trade, not an oversight — the
    label gate is scoped precisely so a greedy Title-Case match can't swallow clinical prose
    (`"pt: severe sepsis with lactate elevation"` → `"patient"` would destroy the query before
    retrieval; see the comment at `deidentify.py:44-49`). The bounded blast radius: the scrub
    protects the **retrieval query egress only** (Voyage/Cohere), the two vendors that never need a
    name; Claude — which does receive full PHI including document images — is covered by a BAA, not
    by this scrub. **The choke-point is a real architectural control (one function, one place, every
    query routed through it — `retriever.py:150`), but it is a shape-based scrub with a known
    free-text-name gap. Do not describe it as de-identification in the HIPAA Safe Harbor sense.**
    Closing the gap needs an NER pass or a chart-name denylist (patient names are known per
    request) — neither is built.
- **Data at rest:** SMART tokens remain Fernet-encrypted; extracted facts + page images are PHI →
  access-controlled behind the same auth and audited (`audit_log` rows for `document.ingest`,
  `extraction.run`, `guideline.retrieve`, plus existing write-propose/commit).
- **Fail-closed verification** across all three citation types is the primary safety control; the
  critic is additional.
- **Secrets** (`VOYAGE_API_KEY`, `COHERE_API_KEY`, write creds) live only in the gitignored droplet
  `.env`; never committed.
- **CI PHI-check:** `no_phi_in_logs` rubric + a PHI-detector scan over logs/traces/eval artifacts;
  extends to "no PHI reaches the reranker/embedder."

## Rationale & alternatives considered

(Full reasoning + tradeoffs in `04-technical-decisions.md`. Summary so a builder doesn't reverse a
deliberate call:)

- **Hand-rolled supervisor, not LangGraph** — grain-fit with the existing Stub/Claude Protocol
  pattern, zero PHI-telemetry surface, trivially LLM-free CI, no framework churn; the doc blesses
  "another inspectable orchestration framework" and grades comprehensibility. Inspectability is met
  via typed logged handoffs + nested spans.
- **OCR + Claude vision, reconciled** — Claude vision doesn't emit reliable pixel boxes; OCR
  provides accurate boxes + independent corroboration; reconciliation doubles as the deterministic
  document-grounding anchor. Local OCR keeps PHI in-container.
- **Agent-store-authoritative grounding** — labs are not API-writable (verified), so lab facts
  cannot be FHIR resources; grounding re-checks the stored schema-validated extraction. OpenEMR
  still stores the source doc.
- **Voyage + Cohere** — highest retrieval quality (Voyage is the Claude-paired embedder); both get
  only de-identified queries. Local zero-egress was rejected for the torch weight vs. lean container.
- **Two-tier eval gate** — the blocking gate must be deterministic (the doc forbids live API in CI
  and the graders inject a regression); a separate live run measures real model quality.
- **SVG-over-image overlay** — inputs are scans, so pdf.js's text layer is moot; we already produce
  page PNGs; hand-rolled SVG fits the house style and adds no dep.

## Assumptions & open questions

- **Model ids and cost rates are settled — DONE.** `claude-sonnet-5` is the real configured model,
  not a placeholder: it is the default for both synthesis/chat (`copilot/config.py:242`) and vision
  extraction (`copilot/config.py:249`), with gating on `claude-haiku-4-5-20251001`
  (`copilot/config.py:245`). Every model the agent calls has an explicit row in
  `copilot/observability/pricing.py:33-37` — `claude-sonnet-5` (3.0/15.0), `claude-haiku-4-5-20251001`
  (1.0/5.0), `voyage-3.5` (0.06/0.0), `rerank-v3.5` (0.25/0.0, a documented per-search-unit
  normalization) — so no call resolves the unknown-model fallback. These are the rates
  `COST_ANALYSIS.md` is sourced from.
- Guideline **corpus content**: curate a small hospitalist-relevant set (e.g., DKA, sepsis, AKI,
  anticoagulation) from openly licensed sources; store source files + ingest script in-repo.
  License each chunk.
- Extraction **confidence threshold** for document grounding is a tunable knob; start conservative,
  let the `factually_consistent`/`citation_present` rubrics calibrate it.
- Physician write-back enablement (`writeback_enabled=true` + write-client scopes) is an operator
  step, already documented in the Week 1 deploy runbook.
- Page-image storage is agent-DB `bytea` for demo scale; MinIO object store is the noted scale path,
  not built.
- **`document_ingestion_enabled` is a real ingestion kill switch, on by default — DONE.** Declared at
  `copilot/config.py:315` with `default=True`, and genuinely enforced at
  `copilot/api/routes/documents.py:126`: when false, `POST /v1/documents` returns **503**
  (`_INGESTION_DISABLED_DETAIL`, `documents.py:55`) and no document is accepted or stored. It gates
  the **upload surface only** — the pipeline service stays directly invocable (tests, CLI,
  background jobs) and already-ingested documents remain readable. It defaults `True` because
  ingestion is a core Week-2 capability that is already live; the flag exists so an operator can
  actually stop intake (e.g. pending an incident). Plumbed through deploy as
  `COPILOT_DOCUMENT_INGESTION_ENABLED` (`docker-compose.deploy.yml:199`, defaulting `true`), and
  locked by two tests (`agent/tests/test_vision_contract.py:150` asserts the default is on; `:161`
  asserts a disabled deployment 503s). The routes remain protected as before by session auth +
  rounding-list RBAC, like every PHI route. **History:** this field previously defaulted `False` and
  was read nowhere — a phantom switch. That defect is fixed; docs may cite the flag as a control.
- **Intake schema ↔ OpenEMR record types — DONE.** Each intake fact is tagged with a typed
  `IntakeCategory` (`demographic` → `patient_data`, `chief_complaint` → `form_encounter.reason`,
  `medication`/`allergy`/`medical_problem` → `lists.type`, `family_history` → `history_data`) via an
  `IntakeFact(ExtractedFact)` subclass, persisted on `extracted_fact.category` (migration `0007`).
  Lab extraction is unchanged (plain `ExtractedFact`). The `allergy`/`medication`/`medical_problem`
  facts now map 1:1 into the write-back path. Still open: an auto-propose bridge from categorized
  intake facts → write candidates (see `agent/research/week2/04-technical-decisions.md` §10).

## Testing strategy

Three layers, each guarding a documented failure mode:

- **Unit tests** — pure logic, no DB, no network. Cover: the `LabReport`/`IntakeForm` Pydantic
  validators (incl. malformed/partial input → rejection, not coercion — *guards: raw VLM output
  bypassing the schema*); the OCR→value reconciliation (bbox attach + confidence; unmatched →
  `supported=false` — *guards: inventing an unsupported fact*); the `deidentify()` scrub (*guards:
  PHI reaching Voyage/Cohere or logs*); the citation-union (de)serialization + backward-compat
  default to `fhir` (*guards: a Week 1 → Week 2 migration breaking old claims*); RRF fusion ordering
  (*guards: silent retrieval-ranking regressions*).
- **Integration tests** (fixtures + stubs, **no live API** — run in CI) — the full
  ingestion-to-answer path against fixture PDFs/form images + a respx-faked OpenEMR + stubbed
  Claude/Voyage/Cohere. Cover: `POST /v1/documents` → extraction persisted → `GET` status;
  supervisor→worker handoffs produce nested spans; the hybrid retriever returns ranked chunks;
  document/guideline claims pass or are dropped by the extended verifier. Contract tests assert the
  implementation matches `agent/openapi/week2.yaml`. *Guards: ingestion-flow and RAG-pipeline
  wiring regressions; API/spec drift.*
- **Golden-set evaluation** (agent behavior) — the 53-case boolean-rubric suite (13
  `gate_dataset.jsonl` + 40 `golden_dataset.jsonl`; rubrics `schema_valid`, `citation_present`,
  `factually_consistent`, `safe_refusal`, `no_phi_in_logs`), stubbed and deterministic, baseline +
  >5% regression. **Enforced by the GitLab CI `agent:tests` job** (`.gitlab-ci.yml`), which is
  path-gated to pushes touching `agent/**/*`. The committed `.githooks/pre-push` hook mirrors it
  locally but is *inert until a dev opts in* (`git config core.hooksPath .githooks`; unset by
  default), so CI is the gate that actually blocks.
  *Guards: behavioral regressions in extraction/citation/grounding/refusal/PHI on every push — the
  graders' injected regression trips this.*

**Not tested (and why):** live model *quality* is not asserted in the blocking gate (non-deterministic,
needs keys, forbidden in CI) — it's measured in a separate non-blocking live run. Third-party model
correctness (Claude/Voyage/Cohere internals) is out of scope — we test our *handling* of their
outputs, not the outputs themselves. Real-scan OCR accuracy beyond our fixture set is not gated —
covered by the confidence-flagging behavior instead of a pass/fail threshold.

## SLOs & alerting — the latency threshold, chosen and defended

The spec leaves the ingestion SLO as a blank (*"p95 < X seconds"*). Here is the number,
what it is grounded in, and why it is not arbitrary. Alert definitions and on-call
response actions live in `OBSERVABILITY.md` §7 — **these thresholds are the same numbers**;
if the two ever disagree, `OBSERVABILITY.md` §7.1 is the operational source of truth.

| SLO | Signal | p95 target (warn / page) |
|---|---|---|
| **Document ingestion latency** | `doc.ingest` + `extraction.run` span duration, per document end-to-end | **< 12 s** warn / **< 30 s** page |
| **Evidence retrieval latency** | `guideline.retrieve` span duration | **< 800 ms** warn / **< 2 s** page |

### Why p95 < 12 s for document ingestion

**The measurement it starts from.** `agent/scripts/latency_report.py` records the
**LLM-free floor** of the real pipeline (rasterize → OCR → reconcile → append-only
persist), committed at `agent/artifacts/latency_report.json`:

| Path (stub, keyless) | p50 | p95 | n |
|---|---:|---:|---:|
| `doc_ingestion` | **36.0 ms** | **151.5 ms** | 5 |
| `evidence_retrieval` | **1.4 ms** | **2.0 ms** | 5 |

**The reasoning, in one line:** our own code costs ~0.15 s at p95; everything else in the
budget is the vision call, so the SLO is sized around **OCR + vision**, not around us.

1. **The floor proves the deterministic path is not the constraint.** 151.5 ms p95 is
   ~1.3% of a 12 s budget. Any breach is upstream (the vision model) or systemic (CPU
   starvation on OCR) — never "our parsing got slow". That is what makes the threshold
   *diagnostic*: it fires for a reason you can act on.
2. **The real path is dominated by one Claude-vision call.** `COST_ANALYSIS.md` §9c sizes
   real ingestion at **≈ 5 s p50 / ≈ 10 s p95** (one vision call per document, §3c, plus
   ~0.5–1 s/page CPU rasterize+OCR). **12 s ≈ the estimated real p95 (10 s) + ~20%
   headroom** — tight enough that a genuine regression trips it, loose enough that a
   normal 2–3 page scan on a busy droplet does not page anyone at 3 a.m.
3. **30 s page = the "something is broken" line,** ~3× the expected p95 and ~2.5× the warn.
   At 30 s the likely causes are Anthropic rate-limiting/retry storms, a missing
   `tesseract` binary forcing a pathological path, or a rasterize loop on a malformed PDF
   — all real incidents, not noise.
4. **The tail is cheap here, which is why 12 s is acceptable at all.** Ingestion is
   **async and non-interactive** (`POST /v1/documents` → `202` + poll `GET
   /v1/documents/{id}`) and **fails closed** (a failed run persists zero facts). A
   document a few seconds slow is invisible to the clinician. Contrast **chat** at
   **p95 < 8 s** (`OBSERVABILITY.md` Alert 2) — a *lower* threshold for a *slower-bounded*
   path, because a clinician is waiting on it. The SLO tracks who is blocked, not which
   pipeline has more steps.
5. **Retrieval's < 800 ms is set the same way:** measured floor 2.0 ms p95 (DB fusion +
   in-Python cosine over 19 chunks), real path ≈ 800 ms p95 estimated from two sequential
   network hops (Voyage embed + Cohere rerank, `COST_ANALYSIS.md` §9d). Retrieval is
   **fail-open** — a timeout falls back to the fused order and still answers — so it warns
   at the estimate and only pages at 2 s.

**What this number is not.** It is **not** a measured production p95. The floor is measured
(n=5, stub path); the 12 s is an **SLO-anchored estimate** for the real path, because no
production traces are retained yet. Both `COST_ANALYSIS.md` §9 and this section label every
such cell — and the moment real traffic flows, the same `doc.ingest` spans in Langfuse
recompute it from observed data. **The threshold is a defended starting point to tune
against the first week of real traffic, not a validated measurement**, and it will be wrong
in some direction; the honest move is to say so and to have picked it for stated reasons.

## Failure modes & incident response

Every entry: how to spot it in logs (all searchable by correlation ID), and the recovery action.

| Failure | Signal in logs | Recovery |
|---|---|---|
| **Document ingestion fails** (upload to OpenEMR errors, rasterize/OCR throws) | `doc.ingest.*` event with error + `source_document.status=failed`; `/ready` `document_store` probe degraded | Ingestion is transactional + append-only — no partial extraction persists; surface "couldn't read this document" to the physician, keep the chart usable; retry is idempotent (content-hash dedupe). |
| **Extraction schema violation** (VLM output won't validate) | `extraction.run` with validation error; no `extracted_fact` rows written | Fail closed — the field is dropped, not coerced; the document is marked extracted-with-gaps and the missing fields shown as absent, never invented. |
| **Reconciliation finds no box** (value not on the page) | `extraction.field.outcome` with `supported=false`, low `match_confidence` | Fact is flagged low-confidence/unsupported in the UI (no bbox chip); it cannot pass the document-grounding gate, so it never appears as a verified claim. |
| **RAG returns no results** (empty/low-score retrieval) | `retrieval.miss` event, `hits=0` | The answer proceeds on patient-record facts only and explicitly states no guideline evidence was found — never fabricates a citation; `citation_present` still holds for patient claims. |
| **Reranker/embedder unreachable or slow** | circuit-breaker open event; `/ready` `reranker`/`embedder` degraded | Bounded retry then fall back to fused sparse+dense order (rerank skipped, logged); retrieval SLO alert fires with the documented on-call action. |
| **Supervisor routing error** (loops / wrong worker / no answer) | `worker.handoff` chain in the trace shows the misroute; iteration cap hit | Hard iteration cap returns a safe "insufficient grounded information" withhold rather than an ungrounded answer; the trace reconstructs the exact handoff sequence from the correlation ID. |
| **Verifier can't re-materialize a source** (FHIR re-fetch 401, extraction row gone) | `verification.result` with the claim absent from context | The claim is dropped (fail-closed); if *no* claims survive, the answer is withheld — identical to the Week 1 contract. |
