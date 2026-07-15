# Week 2 — Solution Ideation Deliverables

Architecture design plan for the Week 2 expansion of the Clinical Co-Pilot (multimodal evidence
agent: document ingestion, supervisor + worker graph, hybrid RAG, eval-gated CI). Produced via the
`solution-ideation` workflow (customer → solution → codebase analysis → per-area architecture
decisions → deliverables), grounded in a live read of the Week 1 codebase.

| # | File | Purpose | Audience |
|---|------|---------|----------|
| 01 | [`01-customer-summary.md`](01-customer-summary.md) | Who this is for and the problem it solves | Stakeholders / product |
| 02 | [`02-architecture-spec.md`](02-architecture-spec.md) | Full build spec — components, stack (with versions), data model, contracts, security, rationale | A downstream build agent |
| 03 | [`03-architecture.mmd`](03-architecture.mmd) | Mermaid diagram of the same architecture | Everyone (render it) |
| 04 | [`04-technical-decisions.md`](04-technical-decisions.md) | ADR-style log of the 9 decisions, with benefits/tradeoffs/fragility | The team, later |

**Note:** `02-architecture-spec.md` is the seed of the required repo deliverable `./W2_ARCHITECTURE.md`
(promote its content there at build time).

## Decisions at a glance

1. Orchestration → **hand-rolled supervisor** (house Stub/Claude Protocol style), new `copilot/graph/`
2. Extraction → **OCR (Tesseract) + Claude vision, reconciled** (bbox + confidence, no invention)
3. Citation/verify → **`{fhir|document|guideline}` union**, agent-store-authoritative document grounding
4. Storage → **OpenEMR owns source docs**, agent DB owns derived facts; **physician-confirmed write-back**
5. RAG → **pgvector + Postgres FTS + RRF**, **Voyage embeddings + Cohere rerank**, repo-reproducible corpus
6. Eval/CI → **two-tier**: stubbed deterministic PR-blocking gate + non-blocking live quality run
7. Observability → **Langfuse + hand-rolled status page**; nested spans; JSON logging; graded `/ready`
8. Overlay → **hand-rolled SVG over the page image**
9. Deploy → **pgvector swap** on the existing droplet; auto-mounted route; Caddy body-size bump

Scope is deliberately narrow (2 doc types, 1 supervisor + 2 workers + critic, 1 small corpus, 1 gate).
**Post-MVP (Early Submission) — now BUILT:** third document type (`medication_list`),
contextual-retrieval upgrades (query expansion + heading-aware chunking + section boost), the
write-back auto-propose bridge, and a genuinely keyed `RealCritic`. Still noted scale paths (not
built): ColQwen2/multi-vector indexing and a MinIO/Grafana scale-out.
