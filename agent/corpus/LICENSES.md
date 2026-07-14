# Guideline corpus — per-source license metadata

Every ingestible corpus file in this directory is a Markdown document with a
`---`-fenced front-matter block carrying its own provenance metadata
(`title`, `source`, `license`, `provenance`). The ingest script
(`agent/scripts/ingest_guidelines.py`) persists that metadata onto the
`guideline_document` row, so license and provenance travel with every
retrieved chunk. This file is the human-readable index of the same
information; files without front matter (like this one) are not ingested.

## Sources

| File | Title | License | Provenance |
|------|-------|---------|------------|
| `dka-adult-inpatient.md` | Diabetic Ketoacidosis (Adult) — Inpatient Management | CC-BY-4.0 | Synthesized original text (AgentForge, 2026) |
| `sepsis-early-management.md` | Sepsis and Septic Shock — Early Inpatient Management | CC-BY-4.0 | Synthesized original text (AgentForge, 2026) |
| `aki-inpatient-management.md` | Acute Kidney Injury — Inpatient Evaluation and Management | CC-BY-4.0 | Synthesized original text (AgentForge, 2026) |
| `anticoagulation-warfarin-reversal.md` | Anticoagulation — Warfarin Reversal and Periprocedural Management | CC-BY-4.0 | Synthesized original text (AgentForge, 2026) |

## Licensing statement

All four documents above are **original prose written for this repository**
(synthesized summaries of widely known clinical practice; facts themselves
are not copyrightable and no text was excerpted from any copyrighted
guideline). They are released under the
[Creative Commons Attribution 4.0 International license (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/).
Attribution: "AgentForge clinical co-pilot demo corpus".

**Not for clinical use.** This corpus exists to exercise the retrieval
pipeline (chunking, embeddings, hybrid retrieval) in demos, tests, and
evals. It is deliberately small, is not maintained against current
guidelines, and must never be treated as medical advice.

## Adding a source

1. Add a Markdown file with the required front-matter keys (`title`,
   `source`, `license`) — use the repo-relative path as `source`; it is the
   idempotency key for re-ingest.
2. Only openly licensed (CC-BY / CC0 / public-domain) or original synthesized
   text is acceptable. Record the license per source; never add text whose
   license is unknown.
3. Add a row to the table above, then re-run
   `python scripts/ingest_guidelines.py` from `agent/`.
