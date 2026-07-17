## Cycle 1
- Scope-check task branches against their BRANCH-BASE, not main. A worktree created before a later main-side commit (e.g. the deps commit) shows those files in `git diff main..branch` as base-lag, not scope creep; three-way merge resolves them cleanly. Verify with `git diff <base>..<branch>`.
- F1 pinned the Citation union via `SkipValidation[FhirReference]` on `Claim.source_ref` to avoid editing out-of-scope legacy readers (`rounds/summary.py`, `chat/service.py`) under strict mypy. DOWNSTREAM CONTRACT: construct concrete `DocumentCitation`/`GuidelineCitation` and assign to `source_ref`; the single cast seam is `repository._claim_from_json`. Repo accessors: create_/get_ for source_document, document_page(get_document_pages), extraction, extracted_fact(get_extracted_facts), guideline_document, guideline_chunk(get_guideline_chunks). Schemas in `copilot.domain.documents`: ExtractedFact/LabReport/IntakeForm (strict, extra=forbid, frozen).
- F2 config fields (COPILOT_ prefix): voyage_api_key, cohere_api_key (both ""=stub), anthropic_model_vision=claude-sonnet-5, voyage_embedding_model=voyage-3.5, cohere_rerank_model=rerank-v3.5, ocr_language=eng, ocr_dpi=200, doc_extraction_confidence_threshold=0.7, document_ingestion_enabled=False (gates the HTTP ROUTE only, not the service). Pricing keys added: voyage-3.5, rerank-v3.5.
- Coupling found: ingestion crit 7 part B drives dedupe through attach_and_extract AND upload_document together -> F3(pipeline)+F4a(upload_document) must be ONE task, not parallel.
## Cycle 2
- Disjoint ownership held again: 2 waves / 6 tasks, ZERO merge conflicts. Pre-dispatch ownership mapping remains the highest-leverage step.
- Baseline-count confusion cost worker attention: several ran `pytest tests` (=504) and doubted the "515" figure. project_tests metric = `pytest tests evals` (515, grows as evals are added, e.g. 521 after F10a). Tell workers the metric counts tests+evals so they do not chase a phantom regression.
- F3+F4a note: the ingestion pipeline builds its write client via build_write_client (gated on writeback_enabled). F8 route-wiring must pair document-ingestion-on with a writable client or uploads fail. F3 dedupe uses a scoped in-pipeline select (F1 repo has no find-by-hash); a get_source_document_by_hash accessor would let it move behind the repository.
## Cycle 3
- 3rd straight zero-conflict cycle; disjoint ownership + per-task self-verified gates continue to hold.
- Recurring: by-id repo accessors are missing (get_extracted_fact / get_guideline_chunk / get_source_document_by_hash); F3 and F5 each added a scoped in-module SELECT. Non-blocking (no metric depends on it); a small consolidation task on repository.py could retire all three behind accessors.
- F8a must widen api/routes/writes.py ConfirmRequest.candidate to AnyWriteCandidate so F4b issue-writes (medical_problem/allergy) can be confirmed over HTTP (currently 422 at parse).
- Split F8 into F8a (routes/OpenAPI/ready) + F8b (observability/status/SLO/packaging) to keep each within one context and file-disjoint.
## Quality-review (cycle 3) — findings triaged
- [FIX / completeness, NOT a metric mover] graph/ is DEAD in prod: build_graph/AgentGraph are referenced only inside graph/ + test_graph.py; api/chat/worker never construct or run it. feat_graph is green-but-unreachable. REQUIRED: wire build_graph into the /v1/chat request path (chat/service.py, behind a flag) so the deployed agent actually uses the supervisor/worker/critic graph. This is the "all-green != engagement complete" trap — must finish even if the loop hits terminal. Schedule C4 (if loop continues) or as post-termination follow-up.
- [quality] SQL-leak is 6 sites / 5 modules, NOT 3: documents/pipeline.py:252, verification/serve.py:168+:222, graph/intake_extractor.py:45, rag/ingest.py:228, rag/retriever.py:139. Consolidate behind new MemoryRepository accessors (get_source_document_by_hash, get_extracted_fact, get_guideline_chunk, get_latest_extraction, find_guideline_document_by_source, all_guideline_chunks). No metric depends on it, but it raises conflict/cost risk on any memory/models.py + repository.py schema change (both hotspots).
- [quality] Real{Critic,EvidenceRetriever,IntakeExtractor} mirror their stubs (self._settings unused in 2; RealIntakeExtractor.ingest_content has zero callers). Keyed prod behaves exactly like keyless for the graph layer. Give Real a genuine behavior or collapse the dual; delete/wire ingest_content. (Note: real Claude VISION still runs via the F3 pipeline the intake-extractor wraps — this is only the graph-layer LLM augmentation.)
- [HAZARD for codebase-map] SkipValidation[FhirReference] on Claim.source_ref: single cast at repository.py:725; any reader dereferencing .value/.resource_type/.field on source_ref typechecks clean under mypy-max yet CRASHES at runtime on a document/guideline claim — readers MUST isinstance-guard. graph/ is test-only, not an entry point. write_client.py = growing write hotspot (7 endpoint families, 480 lines).
- [quality] dedup _ISO_DATE_RE (verification/writes.py:39 + writeback/service.py:558; confirm begdate not double-validated); tokenizer regex dup (agent/stub.py + rag/_lexical.py, cosmetic).
- CLEAN (held): deidentify() is a single choke-point (no PHI-regex dup); AnyWriteCandidate union growth is disciplined; config.py section-organized.

## Cycle 3 integration note — frontend node_modules sync
- After merging a branch that changes `agent/web/package.json`/lock, the PRIMARY repo's
  `agent/web/node_modules` is stale. `web_check.py` runs `npm ci` ONLY when node_modules is
  ABSENT, so a stale-but-present node_modules makes it read 0/6 (vitest bin missing) even though
  the branch verified 6/6 in its own worktree (which had its own npm install).
- FIX at integration: run `npm ci` in the primary `agent/web/` after the merge, before measuring
  feat_frontend. Verified: 0 -> 6 after `npm ci`. Not a code regression — pure env sync.

## Cycle 5 (post-loop guarded cleanup — deferred backlog #1 + #2)
- Interrupted mid-cycle in a prior session; resumed cleanly from artifacts alone (`swarmloop.py
  resume` + state.json/history.csv/worktree list). Both c5 branches were already merged (unmerged=0);
  what was left undone was the post-merge guardrail MEASURE, the cycle record, and worktree cleanup.
  Lesson: after a resume, re-derive "what merged vs what was verified" — a merge landing is NOT proof
  the guardrail ran; the interruption sat exactly in that gap.
- The additive-only cop-out on a consolidation refactor (see LEARNED.md): c5-acc added six repository
  accessors but wired ZERO call sites, leaving them caller-less and the inline SELECTs intact — MORE
  duplication, all-green. Completion needed the wiring half (ba8adb7). Detect with: grep for callers of
  anything a refactor adds; grep that the OLD pattern is gone. The frozen suite guards behavior, not
  DRY-completeness.
- Deferred item #3 (Real* graph-worker realness) intentionally left — validating stub-vs-real behavior
  needs an API-keyed env, which the LLM-free harness deliberately excludes. Not a mechanical fix.
