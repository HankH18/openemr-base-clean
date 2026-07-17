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

## Cycle ledger  (TARGET: stop + final report after cycle 5; keep context <75-80%)

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
| 8 | Hook-bypass security flag on R2-E (`git -c core.hooksPath=/dev/null`) | harness flag | **Verified NO-OP**: core.hooksPath unset, `.git/hooks/pre-commit` absent, `.git/config` clean, diff is docs+collection only. Nothing bypassed. Flagged as a reflex to suppress, not normalized. Merged. |
| 9 | Collection port 8010 (appears NOWHERE in code — only api-collection) → all 24 graded requests connection-refused | audit R2 P0-4 | R2-E: fixed to 8000 in 4 places; verified vs Dockerfile CMD/EXPOSE + README + Caddy. `653b4ae` |
| 10 | W2_ARCHITECTURE bullet: "flag never read anywhere / any doc saying otherwise is wrong" — every clause false | audit R2 P0-5 | R2-E: replaced with truth. Also README two→three doc types; "claude-sonnet-5 placeholder" stale (+ its pricing clause). |
| 11 | `{"facts": []}` validated on all 3 doc models + **4** false docstrings (agent found 2 holes audit missed: medlist after-validator FILTERS back to empty so min_length alone insufficient; `supported` implied bbox unenforced) | audit R2 P1-7 + agent | R2-C: min_length=1 + validator guard + truthful docstrings. 7 required lab fields kept OPTIONAL by design — required-on-an-LLM-schema is hallucination pressure; absence made visible via LabField/missing_lab_fields/incomplete_facts. +19 tests. |
| — | RESIDUAL (flagged, not hidden): `supported=True` with no bbox still validates at the extraction boundary | R2-C | Deferred — enforcing it would red a graph test that isn't encoding this hole; belongs with a graph owner. |
| 12 | **Critic verdict discarded** — unsafe_action still served | R2 P0-3 | R2-B: `_critic_narrowed` filters the verifier's survivors (demote-only by construction). MERGED. |
| 13 | **RESIDUAL I then closed myself**: dropping a claim left the unsafe PROSE in the answer — physician still reads "give 10x insulin", unfootnoted | R2-B flagged | Reason now survives (`_flagged_reasons`); `CriticVerdict.unsafe`; graph path WITHHOLDS the turn. Unknown reason degrades to unsafe. `342a869` |
| 14 | `/v1/status` wrong artifact; SLO cited spans never emitted; `/ready` ok with empty corpus | R2 P1-8/9/10 | R2-D: status→gate_baseline.json (5 real rubrics); doc.ingest/extraction.run/guideline.retrieve spans now REAL; corpus probe→degraded. MERGED. |
| 15 | KNOWN TENSION: `retrieval_hit_rate` has no honest source but the frozen api criterion type-checks it as a number | R2-D | Kept numeric + `retrieval_hit_rate_available:false` + `unavailable:` label. Honest inside the constraint. Live-verified. |
| 16 | **THE GATE NOW MEASURES THE SYSTEM** | R2 P0-1 | R2-A: `evals/live_cases.py` calls real `_answer_inline`/`deidentify`/`verify_answer` keylessly. PROVEN: sabotaging deidentify BLOCKS at 91.38/exit 1 (was 100.0/exit 0). Agent closed a vacuous-refusal hole in its own first attempt. Test-rule forced a better design (rule 5: live cases pass absolutely, immune to tolerance knobs). |
| 17 | D34 PHI check absent from CI | R2 P0-2 | R2-A: blocking `agent:phi` job that GENERATES a real corpus first (0 healthy / 30 detected when neutered) — not a vacuous green. |
| 18 | **Cross-branch semantic collision at merge**: R2-B refactored `_answer_inline` 4-tuple→`_TurnOutcome`; R2-A's live probe unpacked the tuple. git merged clean, GATE went red. | integration | Fixed the caller not the contract. **This is the live tier proving its worth on its first merge** — a fixture gate would have stayed green. `e10a691` |
| 19 | **/v1/status served `eval_by_category: []` in prod** — image COPYed eval_results.json while status.py now reads gate_baseline.json. SECOND time this class shipped. | own live verification | Added the COPY **+ a test that parses both files** and pins them together; verified to bite. `2694853`. Live: 5 mandated rubrics now shown. |
| 20 | **upload_document matched a contract OpenEMR does not have** — required 201 (real: 200), demanded an id from a body that is literally `true`, sent `path` as form-data when the route reads it from the query string. "Store the source document in OpenEMR" (spec req) could not work; masked because write-back is off so DerivedOnlyUploader is substituted. | audit R2 F1 + own PHP-source verification | Fixed all three; OPENEMR_NO_HANDLE sentinel rather than an invented id; +5 tests pinned to the PHP contract. `d68977d` |

### Cycle status
- cycle 1 = deliverable-audit(final) -> wave R2 (5 agents) + unsafe-withhold + artifact-COPY guard. DONE, deployed, live-verified.
- cycle 2 = R3 resilience wave (running) + allergy-reaction + upload-contract fixes. IN PROGRESS.
- cycles 3-5 = audit -> fix -> verify. Then FINAL REPORT (skills used + issues/resolutions).
| 21 | **A failed request emitted NO access log** — the log line lived only on the success path, so the very requests an error rate is computed from were the ones missing. A log-derived dashboard reports a healthy zero while the app fails. | audit R2 P2-17 | Log at ERROR/500 + re-raise untouched; NO exception text (the access trail is PHI-free; the correlation id is the join key). +4 tests incl. a PHI-leak guard. `a7fd52c` |

## Cycle 2 close — issues 22–24

**#22 — I shipped a regression before reading the number.** Over-corrected
`upload_document` from "require 201" to "require 200" after proving real OpenEMR
returns 200 — the same brittleness inverted. Broke the frozen acceptance fake (201):
`feat_ingestion` 8→5, pass-rate 97.83→91.3. Pushed AND deployed `14a572d` before
measuring. Fixed in `f9bc194`: `_require` takes a success SET {200,201}; 404-with-
empty-body and every other status still raise (pinned by an added 202 test).
**Lesson: measure BEFORE push/deploy, not after. The gate is not a formality.**

**#23 — Remote names were backwards in my head.** `origin`=GitHub, `gitlab`=GitLab.
I pushed to a nonexistent `gh` remote, read "pushed both remotes" from my own echo,
and would have left GitLab — the GRADED remote — on the regression commit. Only
`git ls-remote` per remote caught it. **Lesson: verify remote SHAs, never trust an
echo that runs regardless of outcome.**

**#24 — A pipe masked a failed fetch and reset the droplet to a stale ref.**
`git fetch origin main -q 2>&1 | tail -1 && git reset --hard origin/main` — the pipe
made `tail`'s exit status the chain's, so the `&&` proceeded despite the fetch failing
on missing GitLab creds, resetting the checkout to a months-stale `origin/main`
(c463e88). Recovered via a public GitHub mirror remote. Live service was never
affected (containers held the old image). **Lesson: never `&&` after a piped command
whose success matters; use `set -e` + unpiped commands.**

Deploy verified live on `f9bc194`: eval_by_category=5 entries, error_rate 0.0,
p95 151ms, UI 200. Public host is `agentforge.hankholcomb.com` (NOT copilot.*).

## Cycle 3 — audit findings (issues 25–30)

Two independent read-only auditors, every finding backed by a live probe.

**#25 — REAL VISION BREAKS THE FLAGSHIP DEMO DOC (found by me, live).** Ran the three
real demo PDFs through real Claude vision inside the DEPLOYED container. Lab (37 facts)
and med list (12) passed; `sample_intake_form.pdf` FAILED:
`facts.22.value_frequency: Extra inputs are not permitted [input_value=None]`.
The model invented a key that exists in NO schema, with a None value, on 1 fact of 42 —
and `extra="forbid"` discarded the whole extraction. A re-run was clean: INTERMITTENT.
Fixed `67f9801` (drop null-valued extras; a valued extra still raises).

**#26 — The graph didn't own its own withhold. LIVE METRIC CORRUPTION.** Graph returned
`served/passed=True` + full prose on turns its critic called unsafe; the withhold lived
only in ChatService. `record_verification` fired on the un-withheld verification, so
every unsafe withhold was logged to the safety dashboard as `served` — the metric that
proves the safety pass fires reported its opposite. Graph mode IS on in prod (verified
`chat_graph_enabled: True`). Fixed `dfb1b8e`.

**#27 — Reconciliation cannot match ANY multi-word value.** `reconcile.py` scores one
OCR token at a time, but Tesseract emits WORD-level tokens. "Marisol Quintanilla",
"Shortness of breath" -> supported=False, bbox=None. So honest, on-the-page extractions
are reported UNVERIFIED, the bbox overlay draws nothing for intake/med docs, and live
`extraction_field_pass_rate` sits at 0.606. lab_pdf escapes because "13.5" is one token
— which is why the demo path looked fine. `reconcile_value` had ZERO direct tests.
DISPATCHED.

**#28 — Browser upload 400s on the DEFAULT config.** The web client never sends
`clinician_id`; `deps.py` 400s without it. `auth_mode=smart` (the live host) masks it via
cookie — the documented bare-IP demo (`disabled`, the default) is 100% broken through the
UI. The GET route was fixed for this exact class; the POST was left behind. DISPATCHED.

**#29 — Dense retrieval leg unguarded -> HTTP 500.** `retriever.py:176` embeds bare while
both siblings guard. In graph mode (ON in prod) a Voyage outage 500s every guideline
question, where sparse-only could have served. DISPATCHED.

**#30 — Poller telemetry orphaned + correlation id always empty.** `poller.result` is
emitted after its span closes (root-level orphan); `correlation_id_var` is only ever set
by HTTP middleware, so every background tick's audit-row id points at nothing. Hardest
failures emit no result event at all — self-concealing. DISPATCHED.

**Process lesson (mine).** Writing the test for #26 I built an `_UnsafeCritic` that
mapped over the claims it was handed — but the keyless agent grounds no claims, so it
condemned nothing, returned an empty unsafe set, and the test PASSED while proving
nothing. I caught it only because I sabotaged the fix and the test stayed green-ish.
A fixture that derives its output from its input goes vacuous exactly where the input is
empty. Also: my not-guilty verdict on `feat_ingestion` came from measuring, not from
reading the diff.
