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

## HALT CONDITION (updated by the user, 2026-07-17 ~05:00)

Run audit->fix cycles until a cycle finds NOTHING worth fixing, or tokens run out.
NOT a fixed cycle count — the earlier "stop after cycle 5" is superseded. A clean
audit is evidence; a cycle number is not.

## Cycle 3 close — all six findings fixed, deployed, verified live

Commits: 67f9801, dfb1b8e, f8811b2 (self-correction), 2108b2f, 1406442, d2b09c8,
4e2ee9f, 45a31bd. 923 tests green, mypy clean, gate exit 0, feat_ingestion 8,
pass_rate 97.83, frozen harness verified intact across 4 parallel agents.

**Live proof on the DEPLOYED build (real Claude vision, real demo PDFs):**
  6/6 intake-form extractions succeeded (38-46 facts each) — was failing ~1 in 4.
  Lab 37 facts, med list 12 facts, all STRICT SCHEMA OK.

**A test edit I REFUSED.** The tool-schema trim (drop reconciliation-derived fields
from what the model sees) broke a pre-existing test asserting `input_schema ==
model_json_schema()`. Revert check: would it still fail if I reverted my change? NO —
so the test was right and my change was wrong. Reverted the trim rather than edit the
test. It was an optional nice-to-have (the auditor said "none needed for
correctness"), and it silently broke a real guarantee: the model would be asked for
one shape and validated against another. Left as a P2 recommendation for a human
decision instead. This is the justify-test-edit rule doing exactly its job.

**Deferred, with reasons:**
- `_tool_schema` trim — needs a human call on the "input_schema IS the pydantic
  schema" contract. Real but optional; not a defect.
- `STUB_MEDLIST_FACTS` encodes bare drug names ("Lisinopril") while the prompt asks
  for "dose and frequency exactly as printed" — the fixture and the prompt disagree
  about what a medication value IS. Deeper stub-blindness; touching it moves frozen
  metrics, so it needs its own measured cycle.

## Cycle 4 — measured proof of the reconcile fix, + one residual (issue 31)

**The n-gram fix works on REALITY** (real Claude vision + real Tesseract + the new
reconciler, run inside the deployed container):

  sample_intake_form.pdf:     47 facts, 40 supported (85.1%)
                              multi-word 28/30 (93%)   <- was structurally 0
  sample_medication_list.pdf: 12 facts, 10 supported (83.3%)
                              multi-word 10/12 (83%)   <- was structurally 0

Live extraction_field_pass_rate is still 0.606 because it reflects the ONE document
row already in the DB, ingested with the old single-token reconciler. It should rise
on re-ingest; the metric lags the fix, it does not contradict it.

**#31 — RESIDUAL, NOT FIXED (deliberate): values that wrap inside a table cell.**
`'QHS (once daily, bedtime)'` is verbatim on the med-list page but wraps a line:

    40 mg PO QHS (once daily,
    bedtime)

Tesseract reads the table ROW-WISE, so the OCR stream is:

    idx 212: ['mg','PO','QHS','(once','daily,','Hyperlipidemia','06/28/2026',...]

— after "daily," it jumps to the NEXT COLUMN; the wrapped "bedtime)" is far away in
the stream. So the tokens are NOT contiguous and contiguous windowing cannot match.
2 of 12 med-list facts (17%) are falsely reported unverified.

NOT fixed, on purpose: it fails in the SAFE direction (unsupported, never invented),
and a real fix needs layout-aware grouping (same-cell / same-line-block), which means
OcrToken must carry Tesseract's block/par/line ids — an OCR-schema change. That is a
feature with its own blast radius, not a bug fix to land unreviewed at 05:00. The
tempting cheap fix -- match the longest PREFIX and call it supported -- is WRONG: it
would claim support for a value whose tail was never verified, which is precisely the
invention the gate exists to prevent.

Backlogged for a human call, with this evidence.

## Refresh done — A/B proof of the reconcile fix on the live deployment

Re-ingested the demo docs through the REAL pipeline, mirroring the route's uploader
selection (DerivedOnlyUploader, since writeback is off). Same document, old vs new
reconciler -- a controlled A/B, not an inference:

  extraction 1 (04:46, OLD single-token)  lab_pdf  37 facts  21 supported  56.8%
  extraction 3 (09:48, NEW n-gram)        lab_pdf  37 facts  29 supported  78.4%
  extraction 4 (09:50, NEW)               intake   38 facts  31 supported  81.6%
  extraction 5 (09:50, NEW)               medlist  12 facts   8 supported  66.7%

Live extraction_field_pass_rate: 0.606 -> 0.7095. Still blended with the two stale
extractions (1, 2) I created at 04:46/04:57 with the buggy reconciler. Excluding those,
current capability is 68/87 = 78.2%.

Clearing the stale rows was BLOCKED by the auto-mode classifier (destructive SQL on a
live DB) — correctly. Not worked around. DB backup at /root/copilot-db-backup-0940.sql.
Left for the user's decision.

Also noted (not a defect): `attach_and_extract`'s docstring claims "tests, CLI, the F8
route" use it, but the route constructs DocumentIngestionService directly so it can
inject DerivedOnlyUploader (routes/documents.py:69,155). My first refresh probe used the
wrapper's DEFAULT factory, hit the real write client, and failed with
WritebackDisabledError — my probe was wrong, not the product. Stale docstring only.

## Cycle 4 audit — five findings, TWO LIVE (issues 32-36)

**#32 P0 LIVE — unauthenticated PHI read on GET /v1/conversations/{id}.** Observed with
a live probe: no cookie, no clinician_id -> HTTP 200 + another clinician's free-text
clinical Q&A. The handler never touches the cookie so it CANNOT 401 -- auth_mode=smart
does NOT mask it. Autoincrement ids = enumeration oracle; unknown id returns 200 {[]}.
The suite ENSHRINES it (test_chat_routes.py:317 asserts 200 unauth). Same class as the
already-fixed GET /v1/documents/{id}; conversations were left behind. DISPATCHED.

**#33 P0 LIVE — raw clinician questions shipped to Langfuse.** supervisor.py:254/:385/:382
put task.question (free clinical text) on spans/events; langfuse_backend is a passthrough.
CONFIRMED live on the droplet: all 3 langfuse keys set, graph_enabled=True, active
backend = LangfuseObservability (not Noop). The codebase contradicts itself --
retriever.py:162-163 says "never the query text, which may carry PHI" and honors it,
while its own caller two frames up ships that identical text on the parent span.
deidentify() has exactly ONE call site in the whole codebase; the "THE choke-point before
any third-party egress" claim is FALSE -- it guards the Voyage/Cohere leg only. DISPATCHED.

**#34 P1 LIVE — upsert_sync_state is a read-then-write across an await.** Concurrent
/refresh + poller tick -> IntegrityError -> 500, or last-writer-wins moving the watermark
BACKWARD and pairing it with a mismatched content_hash, so the change-gate skips a needed
re-synthesis and a clinician reads a stale card. Same shape in save_memory_file,
upsert_rounding_cursor, set_last_seen. QUEUED (wave 2 — repository.py is owned by #32).

**#35 P2 — every vital write would 502 (int pid sent to a UUID-keyed encounter route).**
Not live (writeback_enabled=false) and fails CLOSED, so the feature is dead, not
dangerous. The test asserts our own URL back to us. DISPATCHED.

**#36 P2 — idempotency store is TOCTOU.** Concurrent double-confirm -> 2 POSTs observed.
OpenEMR implements no idempotency keys (grepped the whole PHP tree: 1 unrelated hit), so
the header is decorative and the server-side store IS the guard. Masked today only by a
client-side isDisabled. DISPATCHED.

**Auditor's clean list (valuable):** append-only claim TRUE (EncounterService strips
user ids); no write-without-confirm path; retry cannot double-commit (write client is
deliberately excluded from retry_async); Voyage/Cohere egress genuinely deidentified;
session_scope genuinely rolls back; no migration/model drift except two index-only items;
document routes authz clean; cross-patient dedupe clean.

**Related, filed as design not defect:** is_authorized gates on a list the CALLER supplies
(rounds.py -> rounding cursor). There is no assignment/roster concept anywhere (grep for
assigned|care_team|roster|panel -> zero authz hits), so POST /v1/rounds/start
{"patient_ids":[B]} self-grants B. authorization.py:5-7 is honest that authorized ==
self-established. So fixing #32 buys a SESSION SCOPE, not an authz boundary. That is a
Phase-1 architecture decision for the user, not something to redesign at 06:00.

## Both P0s CLOSED and verified live (8c9f616, 6996857)

Deployed and verified by exploiting them, not by trusting the build:

  GET /v1/conversations/1  ->  HTTP 401     (was 200 + another clinician's PHI)
  GET /v1/conversations/2  ->  HTTP 401     (uniform — no enumeration oracle)
  droplet HEAD: 6996857, container carries the guard (is_authorized x3)
  deployed supervisor.py: "question=task.question" occurs 0 times

Shipped these two AHEAD of the other three in-flight agents on purpose: PHI was
actively egressing to a third-party SaaS on every graph turn, and an unauthenticated
read was live. Waiting for a tidier tree would have meant more leaking.

**Residuals the agents surfaced (NOT fixed — for a human):**
- `routes/documents.py:189/196` has the same 404-vs-403 oracle shape the conversation
  route just unified away. Lower severity (needs valid creds) but same class.
- **`patient_id` rides on nearly every span/event** (it is in the Observability Protocol
  signature) and still reaches Langfuse. A bare PID is a HIPAA identifier going to the
  same no-BAA SaaS. Systemic — changing it means changing the Protocol. THIS IS THE NEXT
  DECISION WORTH MAKING.
- Two docstrings are now stale and will mislead: `graph/contracts.py:49-50` ("payload
  carries ... the query") and `api/routes/chat.py:89-94` ("the raw question, document
  ids"). Both were outside the agents' file boundaries.
- Authorization is patient-level (rounding list), not conversation-ownership: a
  colleague sharing the round CAN read the thread. Defensible as shared-care-team, but
  it is a deliberate choice, not an accident.

**Corrected my analysis (agent was right):** `chat/service.py:282` `question=message` is
NOT a telemetry site — it is AgentTask construction, the graph's legitimate input
contract. The egress was entirely the three supervisor sites consuming that field.

## Issue 37 — P1: the no-invention gate blesses invented values (PRE-EXISTING)

Surfaced by the wrapped-cell agent, then reproduced by me directly at HEAD with the
value's tail ENTIRELY absent from the token list:

  toks = ['40','mg','PO','QHS','(once','daily,']       # no 'bedtime)' anywhere
  reconcile_value('40 mg PO QHS (once daily, bedtime)', toks)
    -> supported=True  conf=0.8220  bbox=[0.1, 0.5, 0.36, 0.01]

`supported` is THE no-invention gate — it means "I located this verbatim on the page,
here is its bbox". It said that for a value whose tail exists nowhere, and handed back a
box that does not cover the missing tail.

Root cause: SequenceMatcher.ratio() is SYMMETRIC (2*matched/(len_a+len_b)), and
_MIN_LEN_RATIO = 0.8/(2-0.8) = 0.667 — so any span that is a two-thirds-length PREFIX of
the value scores >= _MATCH_MIN and passes. Nothing requires the span to ACCOUNT FOR the
whole value.

Concretely: the model hallucinates "Metformin 500 mg PO BID" where the page prints
"Metformin 500 mg PO". The gate marks it supported and gives the UI a citation box that
does not contain "BID". The clinician clicks the highlight to check the schedule and sees
a box that never says it — the exact failure the gate exists to prevent, dressed as
verification.

Pre-existing (predates the n-gram and wrapped-cell work; HEAD~ byte-identical). DISPATCHED:
require asymmetric COVERAGE (matched_chars / len(value)) in ADDITION to similarity. The
supported rate SHOULD drop — some current supports are false. Explicitly told the agent not
to tune the threshold to preserve the numbers.
