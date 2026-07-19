# Orchestrator Handoff — OpenEMR Clinical Co-Pilot audit→fix loop

**Written 2026-07-19 for a fresh orchestrating agent taking over.** You have no memory of
this run. Read this top to bottom before doing anything. The full blow-by-blow is in
`.swarm-loop/overnight-log.md` (long; this is the distilled version).

---

## 0. The one thing that will bite you first: the freeze

The loop halted several times and the user noticed. It was NEVER an API crash. **Your only
re-invocation engine is a background-agent completion notification (or a user message).** If
you close a cycle — commit, push, log — and end your turn with prose like "cycle N+1 next"
*without having dispatched an agent in that same turn*, nothing is pending, nothing wakes you,
and the session goes idle until the user messages.

**Standing rule:** open the next cycle's first `Agent` in the SAME turn you close the current
one, BEFORE writing the closing summary. The summary is the last thing you emit, after the tool
call that guarantees re-invocation. "I'll continue" is a sentence; a dispatched Agent is
continuation.

---

## 1. What this project is

A fork of OpenEMR (PHP, `9000+` files) with a Python "Clinical Co-Pilot" bolted on under
`agent/`. The co-pilot answers grounded clinical questions about a patient, ingests scanned
documents (vision + OCR + reconciliation), retrieves guideline evidence (hybrid RAG), and has a
propose→confirm write-back path. Everything is fail-closed: a claim that can't be verified
against a live FHIR re-fetch is withheld, not guessed.

The work here is an **autonomous audit→fix loop**: spin up adversarial READ-ONLY auditor
subagents, they find real defects with probes, you dispatch fix subagents, verify each fix
BITES (sabotage → red → restore), commit, push, deploy, verify live. Repeat.

**The user's halt condition:** keep running cycles until a cycle finds NOTHING worth fixing, OR
the Anthropic API stops responding. NOT a fixed cycle count. Context pressure is a compaction
event, NOT a stop signal — never stop because context is long.

---

## 2. Exact current state (verify before trusting)

- **HEAD `045a234`** on all three: local, `origin` (GitHub HankH18), `gitlab` (GitLab, the
  GRADED remote). Tree = **9169 files** — always verify with
  `git ls-tree -r HEAD --name-only | wc -l` before AND after any push (see §6, the wipe).
- **Droplet is BEHIND at `37a9faa`** — that is fine, it's cycle 10's code; only the two most
  recent commits are docs (user decisions + this handoff). Redeploy only when you ship code.
- **1278 tests pass, 2 skipped** (the 2 skips need `ANTHROPIC_API_KEY` — expected). mypy clean
  on `copilot`. `evals/gate.py` exit 0. Frozen acceptance harness intact (`swarmloop.py
  verify`). `acceptance/run.py --pass-rate` = 97.83, `--feature ingestion` = 8.
- **~64 fix/feat commits** landed this run (65 distinct defects found and fixed across 10
  cycles). Defect count per cycle: roughly 2, 9, 3, 4, 4, 3 for cycles 5–10 — severity is
  DROPPING (cycle 9 had a live PHI leak; cycle 10 found zero live defects).

## 3. Deploy topology (you WILL use this)

- Droplet `root@198.199.68.21`, repo at `/root/openemr-base-clean`.
- The droplet has NO GitLab creds. It fetches from a `ghmirror` remote (public GitHub). Deploy =
  `git fetch ghmirror main && git reset --hard ghmirror/main && docker compose -f
  docker-compose.deploy.yml build agent && ... up -d agent`. Use `set -e` and UNPIPED commands
  (a pipe once swallowed a failed fetch's exit code and reset the checkout to a stale ref).
- Public host is **`agentforge.hankholcomb.com`** (NOT copilot.*). The agent container has no
  host port; it sits behind Caddy on the internal network. Verify live via the public URL or
  `docker exec openemr-base-clean-agent-1 python -c ...` inside the container.
- **Deployed config:** `auth_mode=smart`, `graph_enabled=true`, `writeback_enabled=false`,
  `poller_enabled=false`, Langfuse keys SET but pseudonym key UNSET, Voyage/Cohere UNSET,
  `ocr_dpi=200`. Migrations are a MANUAL step after deploy (`alembic upgrade head`); `/ready`
  now GATES on migration head, so a forgotten migration shows as 503 with an actionable message.
- Demo credentials live in the droplet `.env` as `OE_USER`/`OE_PASS`. Demo login is real SMART
  OAuth. The three demo PDFs are in `demo/sample_docs/`.

## 4. USER DECISIONS — settled, do NOT re-litigate

- **Langfuse BAA: CLOSED.** Administration can't obtain one; it's out of scope. Treat the
  pseudonym posture as an accepted operating condition, not a blocker. Never re-raise it.
- **Deidentify residual: APPROVED TO BUILD.** Proceed with distill-before-egress (send distilled
  clinical terms to Voyage/Cohere, not the verbatim question). This is the top of the cycle-11
  queue. See §5.
- **Broad-access-plus-audit: DECIDED (2026-07-19) — the intended model, NOT a defect. Do NOT
  re-flag, do NOT auto-fix, and tell your auditors to skip it.** The earlier audit called
  "self-granted authorization" a hole; that was an overcorrection and is retracted. Verified:
  OpenEMR's own ACL is role×category with no patient dimension and no native per-patient
  allowlist; HIPAA minimum-necessary exempts treatment (45 CFR 164.502(b)(2)(i)); the co-pilot's
  `user/*` SMART token already grants all patients, so the rounding cursor is a NARROWING on top,
  not an escalation; and every PHI read already writes a complete append-only `audit_log` row.
  A hard per-patient block was rejected (fights workflow, breaks the demo on empty care-team
  data — the demo DB has 0 care teams / 0 assigned providers). An optional break-glass MARKER
  (advisory, native FHIR CareTeam, ~1 day incl. seeding demo data) is the documented path IF a
  future operator wants least-privilege, but it's a feature request, not a bug. **Every auditor
  packet must list "self-granted authorization / caller-supplied rounds/start patient list" as a
  KNOWN-ACCEPTED design decision to skip.**

## 5. OPEN WORK QUEUE (cycle 11 onward)

**Approved and ready to build:**
1. **Distill-before-egress for RAG (deidentify residual, USER-APPROVED).** `retriever.py` ~:240
   sends the deidentified-but-still-free-text query to the embedder; `evidence_retriever.py:60`
   passes `task.question` verbatim into `retrieve()`. The regex scrubber cannot catch a bare
   unlabelled name (`"Should John Doe get a statin?"`) — proven, documented, honest. Fix: extract
   distilled clinical terms (the query.py concept vocabulary already exists — look at
   `copilot/rag/query.py`) and send THOSE to Voyage/Cohere, so a raw name never reaches egress.
   Keep the scrubber as defense-in-depth. NOT live today (Voyage/Cohere unset) but this is the
   config-flip-away leak.

**Carried forward, unbuilt (re-verify each is still real before fixing):**
- `_distance_to_range` treats the reference bound as INCLUSIVE, so troponin 0.04 vs a "<0.04"
  band scores distance 0 and a 4x rise classifies `trend=steady` while `severity=warning`. The
  card no longer lies about the number (fixed) but "steady" is arguably its own defect.
- `health_uri /ready` belongs in the deployed Caddyfile — right now Caddy routes to the agent
  without consulting readiness, so an unready container still gets traffic. (Docker healthcheck
  stays `/health` deliberately; the real routing gate is the Caddyfile, which is on the droplet,
  not in the repo.)
- `test_reconcile_multiword.py:548` was flagged as possibly vacuous — never reviewed.
- Coverage counts CHARACTERS, so an invented word whose letters are a subsequence of adjacent
  text can still pass the no-invention gate. Documented in-code; needs word-level alignment to
  close (measured, a min-run-length hardening was tried and rejected).

**Accepted residuals — do NOT "fix" these (they are by-design or the user's call):**
- in-process idempotency + proposal store (single worker; documented; the Dockerfile runs one
  worker so it's sufficient — but adding `--workers` silently voids it, which is written down).
- pseudonym != de-identification (BAA closed per §4).
- rasterize accumulates PNGs in a list (within the 1g mem_limit; streaming is the next bound).
- the demo corpus is single-page — same blind spot as StubOcr's fixture; RELEVANT to the
  handwriting/variant-document work below.

**User's GATED backlog (ONLY after a clean cycle, only if tokens remain):**
1. Non-standard document variants (multi-column, rotated, faxed, photographed).
2. Handwriting recognition. **CRITICAL design note:** the no-invention gate is bottlenecked on
   TESSERACT, not the VLM. Claude vision reads the page; `supported` is granted only when the
   value is found in TESSERACT's tokens. If tesseract can't read handwriting, every handwritten
   fact is unsupported no matter how well vision read it. That is the central design question —
   not a threshold to tune. The 0.95 coverage threshold was calibrated on CRISP 200-DPI renders;
   a fax/photo pushes OCR confidence down and it will start rejecting honest values.
3. The reverse/prune "bloat" loop — Phase 9 of the swarm-loop skill: PROPOSE removals, a
   DIFFERENT agent tries to prove each is ALIVE, then YOU approve personally. Uncertain ⇒ keep.
   The asymmetry: dead code costs a line; removing live code costs an outage. Never symmetric.

## 6. Hard-won operational rules (each cost real pain this run)

- **Verify the EFFECT, never an echo.** `git push A; push B; echo "pushed both"` lied when a
  remote didn't exist. Read back each remote's SHA with `git ls-remote`.
- **`git ls-tree -r HEAD --name-only | wc -l` before every push.** An emptied INDEX turned
  `git add <file> && commit` into a repo WIPE (9159 files → 1), pushed to both remotes, and the
  test suite stayed 100% GREEN throughout because pytest reads the DISK and git reads the INDEX.
  I misread the `git diff --stat` "9160 files deleted" as "docs-only". An auditor caught it. If
  `git status` shows everything as untracked (`??`) rather than modified, the index was emptied.
- **A file-sync keeps scattering `" N.py"` duplicate snapshots.** They're now `.gitignore`d, but
  if pytest's collection count looks wrong, run
  `find agent -regex '.*/tests/.* [0-9].py'` and delete them (diff against the original first).
- **Measure BEFORE you push, never after.** Run the frozen `acceptance/run.py` + `evals/gate.py`
  and READ the number before pushing. A regression pushed-then-discovered costs a correction
  across every remote and host.
- **A config edit is an untested code path.** `${VAR:-}` injects an empty string and pydantic
  rejects `""` for typed fields → the container won't boot. 8 of 9 knobs did this. There's now a
  `_blank_env_means_unset` validator in `config.py` — but always construct Settings with the
  deploy's exact (all-unset) env and watch it boot before shipping a compose change.
- **When parallel agents share a file, `git add <path>` can't separate their work.** Three
  agents edited `chat.py`/`service.py` at once and one fix got swept into another's commit.
  Either SEQUENCE same-file agents, or commit the shared file ONCE with a message covering every
  fix. Don't pretend path-staging isolated one agent's change.

## 7. Agent-orchestration playbook that WORKED

- **Every auditor packet:** READ-ONLY, no `.swarm-loop/` edits, no `archive/` reads, "nothing
  found is a VALUED answer, a fabricated finding is worse than none", every finding carries a
  file:line or probe output, distinguish OBSERVED from INFERRED, say if it's live in the deploy.
  ALSO give every auditor the KNOWN-ACCEPTED skip list (§4/§5) so they don't burn a cycle
  re-finding settled decisions — most importantly, **broad-access-plus-audit is intended, not a
  defect**: an auditor will otherwise "discover" that `rounds/start` takes a caller-supplied
  patient list and flag it as a live authz P0. It is not. Tell them upfront.
- **Every fix packet:** the diagnosis with file:lines, PROVE IT BITES (sabotage IN PLACE with a
  `trap`-guaranteed restore — this venv's `.pth` isn't processed so a throwaway copy imports the
  ORIGINAL and lies; purge `__pycache__` between variants; `ast.parse` the sabotaged file so an
  IndentationError isn't mistaken for a red), append-only tests, the file-scope prohibition list,
  never `git stash` (repo-global, has destroyed work).
- **Four agents died on API 529 mid-task this run.** Their code imported/typechecked/passed the
  suite while its central claim was UNPROVEN and no tests were written. FINISH a dead agent's
  work by hand and prove it bites — never inherit its green. (One "fix" turned out load-bearing
  for only 1 of 2 queries it claimed — visible only by running the check it died before running.)
- **Agents correcting your instructions is the BEST signal.** Multiple refused wrong directives
  with evidence (a host-equality auth rule that contradicts SMART; a "strictly harmful" claim
  that was mis-attributed; a migration verification of mine that was a lying green against an
  in-memory DB). Reward this; it's the loop working.
- Load the `stub-blindness`, `justify-test-edit`, `verify-the-deploy` skills — they encode the
  taxonomy of how a green suite lies, the test-edit burden of proof, and the deploy traps. All
  three were WRITTEN/hardened during this run from real incidents.

## 8. Where the truth lives

- `.swarm-loop/overnight-log.md` — the full issue ledger (65 issues) + user decisions + freeze
  root-cause + the gated feature backlog with design notes.
- `.swarm-loop/HANDOFF.md` — this file.
- `swarmloop.py verify|measure|analyze|checkpoint` — the frozen metric harness; every number
  comes from here, never eyeball a trend.
- The skill's `LEARNED.md` (`~/.claude/skills/swarm-loop/LEARNED.md`) — 37 distilled lessons; it
  is near the ~40 cap and should be CONSOLIDATED (merge duplicates) at the end of the run, not
  mid-flight.

**First action when you take over:** verify §2's state numbers yourself, then dispatch cycle
11 — lead with the approved distill-before-egress build (§5.1) plus one fresh adversarial
auditor on a surface not yet deeply probed (the PHP/Twig fork's login restyle and the FHIR
write PATH semantics against real OpenEMR are the thinnest-covered). Open the agents in your
FIRST turn (§0), then summarize.
