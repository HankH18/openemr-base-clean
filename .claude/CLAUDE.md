<!-- CODEGRAPH_START -->
## CodeGraph

In repositories indexed by CodeGraph (a `.codegraph/` directory exists at the repo root), reach for it BEFORE grep/find or reading files when you need to understand or locate code:

- **MCP tool** (when available): `codegraph_explore` answers most code questions in one call — the relevant symbols' verbatim source plus the call paths between them, including dynamic-dispatch hops grep can't follow. Name a file or symbol in the query to read its current line-numbered source. If it's listed but deferred, load it by name via tool search.
- **Shell** (always works): `codegraph explore "<symbol names or question>"` prints the same output.

If there is no `.codegraph/` directory, skip CodeGraph entirely — indexing is the user's decision.
<!-- CODEGRAPH_END -->

<!-- DOC_CURRENCY_START -->
## Document currency (active vs. archived)

**The rule (enforced by a `PreToolUse` hook):** if a doc file is **not** under `archive/`, it is
**current and authoritative**. Everything under `archive/**` is **historical / superseded** — do
**not** read, grep, or load it unless a human explicitly asks to access or compare past-week work
(then unlock via the `/doc-archive` skill, and `lock` when done). Superseding a doc = move the old
version into `archive/week-N/` **in the same change** that adds the new one; never leave two versions
on the active surface.

**Active project documents (the current source of truth):**
- *Deliverables* — `W2_ARCHITECTURE.md` (current architecture), `WALKTHROUGH.md` (per-feature
  onboarding + submission-readiness), `AUDIT.md`, `USERS.md`, `COST_ANALYSIS.md`, `README.md`; eval
  set + results under `agent/evals/`.
- *This week's planning / ideation* — `agent/research/week2/` (customer summary, architecture spec,
  mermaid diagram, decisions log).
- *Operational / agent docs* — `DEPLOY.md`, `ACCESS.md`, `OBSERVABILITY.md`, `NOTES.md`, `RUNLOG.md`,
  `agent/COMPLIANCE.md`, `agent/LANGFUSE_SETUP.md`, `agent/README.md`.
- *Not ours (upstream OpenEMR — leave as-is):* `API_README.md`, `FHIR_README.md`, `CONTRIBUTING.md`,
  `CHANGELOG.md`, `DOCKER_README.md`, `CODE_OF_CONDUCT.md`, `README-Isolated-Testing.md`.

Historical records live in `archive/` (see `archive/README.md`).
<!-- DOC_CURRENCY_END -->
