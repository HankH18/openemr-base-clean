<!-- ────────────────────────────────────────────────────────────────────── -->

# AgentForge Clinical Co-Pilot

A conversational agent that helps a **hospitalist** prep and round on their patients. It runs as
a **separate Python service** that reads (and, when enabled, writes) patient data **only** through
OpenEMR's FHIR/REST API — no path bypasses OpenEMR's authorization. **Demo data only — never real
PHI.**

- **Week 1 baseline** opens on the *most acute* patient, gives grounded source-cited chart
  summaries and "what changed overnight," answers follow-ups with fail-closed verification
  (**withholds** rather than guesses), and flags a not-yet-seen patient who deteriorates.
- **Week 2** turns it into a **multimodal evidence agent**: it *sees* scanned outside documents,
  extracts strict-schema facts with pixel-level provenance, backs answers with guideline evidence
  via hybrid RAG, and routes work through a supervisor + worker + critic graph — all defended by a
  PR-blocking eval gate.

> **Graders — fastest path:** the deployed demo at **https://agentforge.hankholcomb.com** already
> runs the full Week-2 flow (chat graph on). To run it yourself, see
> [How to run the Week-2 flow](#how-to-run-the-week-2-flow) — the
> [environment-variable table](#week-2-environment-variables) names every flag, and **the core
> Week-2 flow runs fully stub-safe with no external API keys**.

## Week 1 baseline — Rounding Co-Pilot

The accepted Week 1 system, still the foundation everything else builds on:

- **Rounds** — opens on the most-acute patient, with a grounded source-cited chart summary and an
  overnight-change digest; sickest-first ranking across the census.
- **Cited chat** — every claim traces to a record; a chat answer is either *served* or *withheld*
  (a partial-grounding `degraded` verification escalates to a whole-turn withhold before the reply
  is returned). A deterministic **fail-closed verifier** re-materializes and re-checks each claim's
  cited source, or drops it; if no claims survive, the answer is withheld.
- **Memory with provenance, temporal Q&A, per-metric observation series** and trend charts; a
  background **poller** flags silent deterioration.
- **Per-physician SMART login** (`COPILOT_AUTH_MODE`, default `disabled` in code; **on** in the
  deploy) and **physician write-back** (`COPILOT_WRITEBACK_ENABLED`, default **off**) — both
  flag-gated.
- **Rounds Co-Pilot UI** (`agent/web/`, React 18 · Vite · TypeScript · React Aria) — grounded
  cards with provenance chips, cited chat, trend charts, deterioration alerts, light + dark.

Stack: `agent/` (Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy/Postgres · Anthropic Claude —
synthesis `claude-sonnet-5`, gating `claude-haiku-4-5`). Deterministic test suite green with no key
or network; full E2E acceptance suite passing.

## Week 2 — Multimodal Evidence Agent

New this week (authoritative design: [`W2_ARCHITECTURE.md`](W2_ARCHITECTURE.md)):

- **Document ingestion** — `attach_and_extract` / `POST /v1/documents` accept three document types,
  `lab_pdf`, `intake_form` and `medication_list`. Pipeline: PDF rasterize → **Tesseract OCR** →
  **Claude-vision** structured extraction → strict **Pydantic schema** validation (a field that
  doesn't validate is rejected, not coerced) → OCR reconciliation (attach bounding box + match
  confidence). The source document is stored in OpenEMR (`POST /api/patient/:pid/document`);
  derived extractions/facts are **append-only** in the agent DB. Every extracted fact carries a
  **per-fact citation with a page bbox**; a value that can't be located on the page is flagged
  `supported=false`, never invented.
- **Hybrid RAG + rerank** over a small hospitalist guideline corpus (4 documents / 19 chunks) —
  sparse (**in-process BM25**; the legacy Postgres FTS leg is inert) + dense (Voyage `voyage-3.5` embeddings; **pgvector stores** the vectors,
  ranking is a **Python-side cosine over the loaded chunk set** — no vector operator, no ANN index,
  honest at this corpus size, see [`W2_ARCHITECTURE.md`](W2_ARCHITECTURE.md) §RAG index for the
  scale path) → **RRF fusion** → **Cohere `rerank-v3.5`**. Only **scrubbed** clinical-topic queries
  leave the deployment — never chart facts or document images — via a single `deidentify()` choke
  point. *Stated honestly:* that scrub is a deterministic regex pass that strips structured
  identifiers (MRN/SSN/DOB/phone/email) and label-gated names, **not** arbitrary free-text names;
  it bounds egress but is not Safe Harbor de-identification (see
  [`W2_ARCHITECTURE.md`](W2_ARCHITECTURE.md) §Security). Guideline evidence is returned as a
  **separate, labeled block**, never mixed into patient-fact claims.
- **Multi-agent graph** — a hand-rolled **supervisor** routes to two workers
  (**intake-extractor**, **evidence-retriever**) plus a **critic**, with typed logged handoffs and
  nested spans reconstructable from a single correlation ID. Wired into `POST /v1/chat` behind
  `COPILOT_CHAT_GRAPH_ENABLED` (default off; **on** in the deployed demo). The fail-closed reply
  invariant is identical whether the flag is on or off.
- **Citation contract + provenance UI** — every clinical claim is clickable to its exact source via
  a discriminated union `FhirCitation | DocumentCitation | GuidelineCitation`. The UI shows a
  provenance chip per claim; a document claim opens the scanned page with its bounding box
  highlighted (SVG-over-image overlay).
- **Eval HARD GATE** — a deterministic, PR-blocking rubric gate over a 62-case golden set (53 fixture + 9 live); see
  [Eval suite](#eval-suite).

## How to run the Week-2 flow

The core flow is **upload → extract → hybrid RAG → grounded answer with clickable citations**.

### Deployed (nothing to install)

**https://agentforge.hankholcomb.com** — chat graph **on**, per-physician **SMART sign-in** (sign
in with an OpenEMR physician account; demo credentials handed off separately — see
[`ACCESS.md`](ACCESS.md)). Live over the seeded 15-patient census.

### Run locally

Bring up OpenEMR (the system of record); full setup, seeding, TLS, and where each credential lives
are in [`ACCESS.md`](ACCESS.md).

```bash
# OpenEMR fork (system of record) — admin/pass, ports 8300/9300
cd docker/development-easy && docker compose up --detach --wait
```

**Option A — mock UI** (frontend only, no backend, fully reproducible local option; the walkthrough video runs the live deployed flow — see [`demo/VIDEO.md`](demo/VIDEO.md)):

```bash
cd agent/web && npm install && npm run dev      # http://localhost:5173  (built-in demo cohort)
```

**Option B — live agent, stub-safe with no API keys** (leave every key blank ⇒ deterministic stub
embeddings / rerank / extraction; no external network):

```bash
cd agent && uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e '.[dev]'
pytest -q                                        # deterministic; no key or network needed
# Enable the multi-agent chat graph; keys stay blank ⇒ full stub path
# (the /v1/documents upload surface is always mounted — no flag needed):
COPILOT_CHAT_GRAPH_ENABLED=true \
  uvicorn copilot.api.app:app --port 8000
#   endpoints: /health /ready /v1/documents/* /v1/chat /v1/rounds/* /v1/writes /v1/patients/{id}/observations
cd agent/web && VITE_API_BASE_URL=http://localhost:8000 npm run dev   # http://localhost:5173
```

For **real** Claude vision + Voyage + Cohere and live OpenEMR FHIR/SMART, set the keys in the table
below and follow [`ACCESS.md`](ACCESS.md) "Run locally → Option B". **Tesseract OCR ships inside the
agent Docker image — no host install.**

### Week-2 environment variables

Read with the `COPILOT_` prefix (pydantic settings — `.env` or shell env). Full option list in
[`agent/copilot/config.py`](agent/copilot/config.py).

| Variable | Default | Effect |
|---|---|---|
| `COPILOT_CHAT_GRAPH_ENABLED` | `false` | Route `/v1/chat` through the supervisor/worker/critic graph instead of the inline verify path. **On in the deployed demo.** |
| `COPILOT_ANTHROPIC_API_KEY` | _(blank)_ | Real Claude vision extraction + synthesis; also gates `/ready`. Blank ⇒ deterministic stub path (no network). |
| `COPILOT_VOYAGE_API_KEY` | _(blank)_ | Voyage `voyage-3.5` guideline embeddings. Blank ⇒ keyless stub embeddings (CI-safe). |
| `COPILOT_COHERE_API_KEY` | _(blank)_ | Cohere `rerank-v3.5` retrieval rerank. Blank ⇒ keyless stub rerank (CI-safe). |
| `COPILOT_DOC_EXTRACTION_CONFIDENCE_THRESHOLD` | `0.01` | Minimal OCR-**legibility** floor: the weakest per-token OCR confidence across the *located* span must clear it for a value to keep its citation bbox. Gates legibility ONLY — whether the value is on the page is decided separately (and confidence-independently) by two-sided coverage + similarity, the real gate. A token OCR marked literal-zero confidence is still withheld; a value below the floor is flagged unsupported, never trusted. (Claim-grounding uses the separate `COPILOT_DOC_GROUNDING_CONFIDENCE_THRESHOLD`, default `0.5`.) |

Week-1 auth / write-back flags (`COPILOT_AUTH_MODE`, `COPILOT_WRITEBACK_ENABLED`), model ids,
OCR language/DPI, and Langfuse creds are also in `config.py`.

### Eval suite

Two **distinct** tiers — keep them straight:

1. **Deterministic grounding / contract tier** — [`eval_dataset.jsonl`](agent/evals/eval_dataset.jsonl)
   (**11 cases**) run by [`run_evals.py`](agent/evals/run_evals.py) against the black-box HTTP
   contract with a fake OpenEMR + stub agent (no key, no network). Recorded results:
   [`EVAL_RESULTS.md`](agent/evals/EVAL_RESULTS.md) (**11/11**). Asserts served/withheld/refused
   decisions, claim citations, temporal grounding, cross-patient isolation, and sickest-first
   ranking. An optional LLM-judge entailment layer
   ([`test_grounding_evals.py`](agent/evals/test_grounding_evals.py)) skips without a key.

2. **HARD GATE — boolean-rubric golden set (PR-blocking)** — the graded deliverable.
   [`gate.py`](agent/evals/gate.py) scores the **union of
   [`gate_dataset.jsonl`](agent/evals/gate_dataset.jsonl) (13) +
   [`golden_dataset.jsonl`](agent/evals/golden_dataset.jsonl) (40) = 53 fixture cases, + 9 live = 62 cases** against **5 boolean
   rubrics** — `schema_valid`, `citation_present`, `factually_consistent`, `safe_refusal`,
   `no_phi_in_logs` — stubbed and deterministic. It **exits nonzero on a >5% relative pass-rate
   regression** vs [`gate_baseline.json`](agent/evals/gate_baseline.json) (baseline 100%).
   `python evals/gate.py --inject-regression` drops the pass rate to 0, proving the gate is
   non-vacuous.

   *What actually enforces it:* the **GitLab CI `agent:tests` job**
   ([`.gitlab-ci.yml`](.gitlab-ci.yml)) is the enforcing backstop and the only automatic gate. It is
   **path-gated** (`rules.changes: agent/**/*`), so it runs on every push **that touches `agent/`** —
   deliberately not on the PHP monolith's churn, which cannot regress agent behavior. A git
   **pre-push hook** ([`.githooks/pre-push`](.githooks/pre-push)) mirrors it locally, but is
   **available, not active**: git ignores `.githooks/` until a developer opts in per clone, and
   `core.hooksPath` is **unset in this repo**, so the hook is **inert until you run**:

   ```bash
   git config core.hooksPath .githooks    # one-time, per clone; bypass once with `git push --no-verify`
   ```

   The hook is a convenience that shortens the feedback loop; it is deliberately not self-installing
   (it would otherwise mutate a shared clone's git config silently). Server-side branch protection
   requiring `agent:tests` to pass before merge is a GitLab project setting — an operator step, not
   configured in this repo. See [`.githooks/README.md`](.githooks/README.md).

   *Scope, stated honestly:* the gate scores **recorded rubric fixtures** — it verifies the rubric
   logic and the fixtures' consistency, and blocks a regression in that scored set. Coverage of the
   **live agent's behavior** comes from the ~1454-case `pytest tests evals` + acceptance suites (also
   run in the same CI job). The two together — behavioral suites + the rubric gate — are what block a
   regression from reaching the demo.

## Docs & submission deliverables

| Deliverable | Where |
|---|---|
| Repository (fork + setup + deployed link) | this repo · run sections above |
| **Week-2 architecture** | [`W2_ARCHITECTURE.md`](W2_ARCHITECTURE.md) — ingestion flow, worker graph, RAG, eval gate, data model, risks |
| Audit document | [`AUDIT.md`](AUDIT.md) |
| User doc + use cases | [`USERS.md`](USERS.md) |
| **Eval dataset + results** | [`agent/evals/`](agent/evals/) — HARD GATE (62-case golden set [53 fixture + 9 live] + [`gate.py`](agent/evals/gate.py) + [`gate_baseline.json`](agent/evals/gate_baseline.json)) **and** the 11-case grounding tier ([`eval_dataset.jsonl`](agent/evals/eval_dataset.jsonl) → [`EVAL_RESULTS.md`](agent/evals/EVAL_RESULTS.md)); see [Eval suite](#eval-suite) |
| **AI cost analysis** | [`COST_ANALYSIS.md`](COST_ANALYSIS.md) — actual dev spend + 100 / 1K / 10K / 100K projections + per-tier architecture changes |
| Deploy runbook | [`DEPLOY.md`](DEPLOY.md) — Week-2 ship steps in §18 |
| Access / run guide | [`ACCESS.md`](ACCESS.md) |
| API collection | [`api-collection/`](api-collection/) — Bruno + Postman |
| Deployed application | **https://agentforge.hankholcomb.com** (chat graph on; per-physician SMART login) |
| **Demo video** | ▶ [Loom walkthrough — Week-2 Medical RAG submission](https://www.loom.com/share/c996666c975248c2a9de2b9f2262799e) ([`demo/VIDEO.md`](demo/VIDEO.md)) · shot list [`demo/W2_MVP_SCRIPT.md`](demo/W2_MVP_SCRIPT.md) |
| Observability + rigor (bonus) | [`OBSERVABILITY.md`](OBSERVABILITY.md) · [`agent/COMPLIANCE.md`](agent/COMPLIANCE.md) · load test [`loadtest/RESULTS.md`](loadtest/RESULTS.md) · [`NOTES.md`](NOTES.md) · [`RUNLOG.md`](RUNLOG.md) |

<!-- ────────────────────────────────────────────────────────────────────── -->

[![Syntax Status](https://github.com/openemr/openemr/actions/workflows/syntax.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/syntax.yml)
[![Styling Status](https://github.com/openemr/openemr/actions/workflows/styling.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/styling.yml)
[![Testing Status](https://github.com/openemr/openemr/actions/workflows/test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/test.yml)
[![JS Unit Testing Status](https://github.com/openemr/openemr/actions/workflows/js-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/js-test.yml)
[![PHPStan](https://github.com/openemr/openemr/actions/workflows/phpstan.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/phpstan.yml)
[![Rector](https://github.com/openemr/openemr/actions/workflows/rector.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/rector.yml)
[![ShellCheck](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/shellcheck.yml)
[![Docker Compose Linting](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-compose-lint.yml)
[![Dockerfile Linting](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/docker-lint-hadolint.yml)
[![Isolated Tests](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/isolated-tests.yml)
[![Inferno Certification Test](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/inferno-test.yml)
[![Composer Checks](https://github.com/openemr/openemr/actions/workflows/composer.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer.yml)
[![Composer Require Checker](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/composer-require-checker.yml)
[![API Docs Freshness Checks](https://github.com/openemr/openemr/actions/workflows/api-docs.yml/badge.svg)](https://github.com/openemr/openemr/actions/workflows/api-docs.yml)
[![codecov](https://codecov.io/gh/openemr/openemr/graph/badge.svg?token=7Eu3U1Ozdq)](https://codecov.io/gh/openemr/openemr)

[![Backers on Open Collective](https://opencollective.com/openemr/backers/badge.svg)](#backers) [![Sponsors on Open Collective](https://opencollective.com/openemr/sponsors/badge.svg)](#sponsors)

# OpenEMR

[OpenEMR](https://open-emr.org) is a Free and Open Source electronic health records and medical practice management application. It features fully integrated electronic health records, practice management, scheduling, electronic billing, internationalization, free support, a vibrant community, and a whole lot more. It runs on Windows, Linux, Mac OS X, and many other platforms.

### Contributing

OpenEMR is a leader in healthcare open source software and comprises a large and diverse community of software developers, medical providers and educators with a very healthy mix of both volunteers and professionals. [Join us and learn how to start contributing today!](https://open-emr.org/wiki/index.php/FAQ#How_do_I_begin_to_volunteer_for_the_OpenEMR_project.3F)

> Already comfortable with git? Check out [CONTRIBUTING.md](CONTRIBUTING.md) for quick setup instructions and requirements for contributing to OpenEMR by resolving a bug or adding an awesome feature 😊.

### Support

Community and Professional support can be found [here](https://open-emr.org/wiki/index.php/OpenEMR_Support_Guide).

Extensive documentation and forums can be found on the [OpenEMR website](https://open-emr.org) that can help you to become more familiar about the project 📖.

### Reporting Issues and Bugs

Report these on the [Issue Tracker](https://github.com/openemr/openemr/issues). If you are unsure if it is an issue/bug, then always feel free to use the [Forum](https://community.open-emr.org/) and [Chat](https://www.open-emr.org/chat/) to discuss about the issue 🪲.

### Reporting Security Vulnerabilities

Check out [SECURITY.md](.github/SECURITY.md)

### API

Check out [API_README.md](API_README.md)

### Docker

Check out [DOCKER_README.md](DOCKER_README.md)

### FHIR

Check out [FHIR_README.md](FHIR_README.md)

### For Developers

If using OpenEMR directly from the code repository, then the following commands will build OpenEMR (Node.js version 24.* is required) :

```shell
composer install --no-dev
npm install
npm run build
composer dump-autoload -o
```

### Contributors

This project exists thanks to all the people who have contributed. [[Contribute]](CONTRIBUTING.md).
<a href="https://github.com/openemr/openemr/graphs/contributors"><img src="https://opencollective.com/openemr/contributors.svg?width=890" /></a>


### Sponsors

Thanks to our [ONC Certification Major Sponsors](https://www.open-emr.org/wiki/index.php/OpenEMR_Certification_Stage_III_Meaningful_Use#Major_sponsors)!


### License

[GNU GPL](LICENSE)
