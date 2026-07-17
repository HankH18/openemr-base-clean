# Overnight audit→build loop (2026-07-17, autonomous, user asleep)

**Mandate:** keep running audit → triage → swarm build → verify → deploy → re-audit cycles until
out of runway. No user checkpoints — ignore every "STOP and confirm" in the skill files; use best
judgment. Final message must list: (1) all skills used + how, (2) every issue found + how resolved.

**Resume contract (if context compacts, read this first):**
- Deadline: FINAL submission **Sun 2026-07-19 noon CT**. Early Sub was Thu 07-16 (done).
- Repo `/Users/hankholcomb/Documents/code_parent_folders/gauntlet_repos/openemr-base-clean`;
  live `https://agentforge.hankholcomb.com`; droplet `ssh root@198.199.68.21` (use the IP — the
  hostname isn't in known_hosts), deploy dir `/root/openemr-base-clean`.
- Deploy = `git fetch https://github.com/HankH18/openemr-base-clean.git main` + `git reset --hard
  <sha>` (droplet has NO gitlab creds) → `build agent` → `alembic upgrade head` → **`python
  scripts/ingest_guidelines.py --force` if the embedder changed** → build web dist LOCALLY + rsync
  (no node on droplet) → `up -d agent caddy`. Always `-T` on `docker compose run` (stdin swallow).
- NEVER `git stash` (refs/stash is shared across worktrees — it swapped two agents' trees).
- Harness: `python3 ~/.claude/skills/swarm-loop/scripts/swarmloop.py verify|measure --cycle N`.
  **11/12 is the expected steady state.** `feat_api` is deliberately **8/9** — the frozen
  `acceptance/api/test_api_02` asserts an UNAUTHENTICATED GET /v1/documents/{id} returns 200, i.e.
  it encodes a PHI hole as the contract. Never "fix" it by weakening auth; never edit the harness.
- Tests are APPEND-ONLY (`justify-test-edit`). Audit removed lines of every branch's test diff
  before merging; reject unjustified assertion changes even when green.

## Cycle ledger

| # | Phase | HEAD | Result |
|---|---|---|---|
| pre | remediation waves 1+2 (9 agents) | 6a2d91d | 11/12, 715 tests, deployed |
| pre | allergy `reaction` silently dropped by OpenEMR whitelist + last pgvector overclaim | 456d9f3 | 720 tests, pushed |
| 1 | independent `deliverable-audit` (stage=final) | 456d9f3 | DONE — 24 deliverables, 5 spec defects, P0 headline: **the eval gate never invokes copilot** |
| 1 | swarm wave R2 dispatched: A gate-measures-system+PHI-CI · B critic-verdict+obs · C schemas · D status/ready/spans · E docs+collection-port | 456d9f3 | running |

## Issue ledger (for the final report — append every item, never rewrite)

| # | Issue | Source | Resolution |
|---|---|---|---|
| 1 | `create_allergy` sent `reaction`, silently dropped by OpenEMR's whitelist (201, no error, reaction never reached the chart) | own verification of OpenEMR PHP source | Folded reaction+provenance into `comments` (the only persisted field) + structural guard test pinning payload keys to the real whitelist. `456d9f3` |
| 2 | Bare "dense (pgvector)" overclaim in superseded research spec | own sweep | Corrected to "pgvector storage; Python-side cosine". `456d9f3` |

| 3 | **THE EVAL GATE DOES NOT MEASURE THE AGENT** — gate.py grades a pre-baked `record` field from JSONL, never imports copilot. Auditor sabotaged deidentify→identity (PHI off) and _passed_claims→[] (all uncited): gate still 100%, exit 0. HARD GATE deliverable is the named artifact graders inspect. | audit R2 P0-1 | wave R2-A: add live-code rubric cases that invoke real copilot; keep fixture tier; prove sabotage turns it red |
| 4 | No PHI-detection check in CI (spec D34 explicitly requires "verify it in CI") | audit R2 P0-2 | wave R2-A: wire frozen `acceptance/phi_check.py` as a CI job |
| 5 | Critic verdict computed then DISCARDED — `_answer_via_graph` never reads `result.critic`; a RealCritic unsafe_action flag is still served | audit R2 P0-3 | wave R2-B: intersect served claims with critic.accepted (demote-only) |
| 6 | API collection port 8010 vs README 8000 → ALL 24 requests connection-refused (graded deliverable silently broken) | audit R2 P0-4 | wave R2-E: verify real port, make collection+docs agree |
| 7 | W2_ARCHITECTURE stale bullet says `document_ingestion_enabled` "is never read anywhere" + "any doc that says otherwise is wrong" — every clause now false; instructs grader to distrust all docs | audit R2 P0-5 | wave R2-E: delete/replace with the truth (real kill switch, default True, 503 when off) |
| 7b | `{"facts": []}` validates on all 3 doc models + 2 FALSE docstrings claiming it can't | audit R2 P1-7 | wave R2-C: min_length=1 + truthful docstrings |
| 7c | `/v1/status` reads the 11-case Week-1 tier → retrieval_hit_rate structurally pinned 0.0; shows wrong categories | audit R2 P1-8 | wave R2-D: point at gate_baseline.json's per_category |
| 7d | OBSERVABILITY SLOs cite `doc.ingest`/`guideline.retrieve` spans that are NEVER emitted | audit R2 P1-9 | wave R2-D: emit the spans (or rewrite the SLO) |
| 7e | `/ready` returns ready:true with an EMPTY corpus (skipped ingest ⇒ zero evidence, no warning) | audit R2 P1-10 | wave R2-D: corpus probe → degraded |
| 7f | Inline (default) path logs 4/7 required observability fields; "tool sequence" is an int | audit R2 F7 | wave R2-B |
| 7g | Duplicate guideline retrieval per graph turn; graph unobservable to a grader | audit R2 P2-13/14 | wave R2-B: single retrieval + expose handoffs |
| — | **DISMISSED** P1-6 "FhirReference carries none of the 5 spec keys" | audit R2 | **WRONG — verified at ground truth**: all 5 serialize via computed_field (auditor read field names, missed the aliases). No action. |
| — | **RESOLVED (was 'unverified')** live chat_graph_enabled state | audit R2 | Verified on droplet: `COPILOT_CHAT_GRAPH_ENABLED=true`. Graph IS live; README accurate. Still exposing it via R2-B (grader can't observe it). |
| — | Pending next wave: timeouts/retries (Voyage/Cohere/OAuth none; 600s Anthropic; critic SYNC client blocks event loop); upload_document expects 201+id but OpenEMR returns 200+bare `true`; no access log on failed requests | audit R2 P1-11/F1/P2-17 | wave R3 |
