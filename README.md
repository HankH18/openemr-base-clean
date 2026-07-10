<!-- ────────────────────────────────────────────────────────────────────── -->

# AgentForge Clinical Co-Pilot

A conversational agent that helps a **hospitalist** prep and round on their patients —
opening on the **most acute** patient first, giving a grounded, source-cited chart summary
and "what changed overnight," answering follow-ups where every claim traces to a record
(and **withholding** rather than guessing when it can't), and proactively flagging a
not-yet-seen patient who deteriorates. Built as a **separate Python service** that reads
patient data **only** through OpenEMR's FHIR/REST API — no read path bypasses OpenEMR's
authorization.

- **Agent service** — `agent/` (Python 3.12 · FastAPI · Pydantic v2 · SQLAlchemy/Postgres ·
  Anthropic). Chat, rounds, background poller, deterministic fail-closed verification, memory
  with provenance. 285 tests; full E2E acceptance suite green.
- **Web UI** — `agent/web/` (React 18 · Vite · TypeScript · React Aria Components). The
  "Rounds Co-Pilot" panel: grounded cards with provenance chips, cited chat with
  served/withheld/degraded states, deterioration alerts. Light + dark.
- **Docs** — [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`AUDIT.md`](AUDIT.md) ·
  [`USERS.md`](USERS.md) · [`COST_ANALYSIS.md`](COST_ANALYSIS.md) ·
  [`OBSERVABILITY.md`](OBSERVABILITY.md) · [`ACCESS.md`](ACCESS.md) ·
  [`DEPLOY.md`](DEPLOY.md) · [`NOTES.md`](NOTES.md) · [`demo/SCRIPT.md`](demo/SCRIPT.md) ·
  build log [`RUNLOG.md`](RUNLOG.md).

## Quick start

```bash
# OpenEMR fork (system of record) — admin/pass, ports 8300/9300
cd docker/development-easy && docker compose up --detach --wait

# Agent service (Python 3.12)
cd agent && uv venv --python 3.12 && source .venv/bin/activate && uv pip install -e '.[dev]'
pytest -q                                   # 285 passing, deterministic (no key/server needed)
uvicorn copilot.api.app:app --port 8000     # /health, /ready, /v1/rounds/*, /v1/chat

# Rounds Co-Pilot UI (runs standalone on the seeded demo cohort — no backend needed)
cd agent/web && npm install && npm run dev
# point it at the live service: set VITE_API_BASE_URL (see agent/web/README.md)
```

**Operator actions to go fully live** (need credentials; intentionally not committed):
`ANTHROPIC_API_KEY` (swaps the deterministic stub agent for live Claude), SMART client
registrations (chat + backend-services poller), Langfuse creds, and deploy. **Demo data only —
never real PHI.**  _Deployed URL: **http://198.199.68.21/** — basic-auth user `demo` (password shared separately)._

## Submission deliverables

| Deliverable | Where |
|---|---|
| GitHub repository (fork + setup + architecture + deployed link) | this repo · [`ARCHITECTURE.md`](ARCHITECTURE.md) · Quick start above |
| Audit document | [`AUDIT.md`](AUDIT.md) |
| User doc + use cases | [`USERS.md`](USERS.md) |
| Agent architecture | [`ARCHITECTURE.md`](ARCHITECTURE.md) |
| **Eval dataset + results** | [`agent/evals/`](agent/evals/) — 10-case hermetic suite ([`eval_dataset.jsonl`](agent/evals/eval_dataset.jsonl)), runner ([`run_evals.py`](agent/evals/run_evals.py)), recorded results [`EVAL_RESULTS.md`](agent/evals/EVAL_RESULTS.md) (10/10), plus LLM-backed [`test_grounding_evals.py`](agent/evals/test_grounding_evals.py) |
| **AI cost analysis** | [`COST_ANALYSIS.md`](COST_ANALYSIS.md) — actual dev spend + 100 / 1K / 10K / 100K projections + per-tier architecture changes |
| Deployed application | **http://198.199.68.21/** (live; basic-auth `demo`) |
| Demo video script | [`demo/SCRIPT.md`](demo/SCRIPT.md) |
| Observability + rigor (bonus) | [`OBSERVABILITY.md`](OBSERVABILITY.md) · load test [`loadtest/RESULTS.md`](loadtest/RESULTS.md) · API collections [`api-collection/`](api-collection/) |

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
