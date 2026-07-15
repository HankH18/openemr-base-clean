# Demo Video — AgentForge Clinical Co-Pilot (Week 2)

**▶ Watch the walkthrough: https://www.loom.com/share/ef4fc41b32f345bab2d9a21e0dd7ebf7**
*"Document Upload MVP, OCR, and RAG Update"*

A walkthrough of the Week-2 multimodal flow on the deployed app (live at
**https://agentforge.hankholcomb.com**, per-physician SMART login):

- **Document ingestion** — upload a scanned lab PDF and an intake form; the pipeline
  rasterizes → runs OCR → Claude-vision structured extraction into strict schemas
  (the sample documents live in [`sample_docs/`](sample_docs/)).
- **Extraction with provenance** — each extracted fact (lab: test name, value, unit,
  reference range, abnormal flag; intake: demographics, chief concern, meds, allergies,
  family history) carries a citation, and a document claim opens the scanned page with its
  bounding box drawn.
- **Hybrid RAG evidence** — guideline evidence retrieved (sparse + dense → rerank) and kept
  as a labeled block, separate from patient-record facts.

Builds on the Week-1 baseline (sickest-first rounds, grounded fail-closed chat).

See [`W2_MVP_SCRIPT.md`](W2_MVP_SCRIPT.md) for the full Week-2 shot list, [`SCRIPT.md`](SCRIPT.md)
for the Week-1 script, and [`../ACCESS.md`](../ACCESS.md) for how to reach the live and local
environments.
