# archive/ — historical, superseded documents (QUARANTINED)

Everything under `archive/` is a **historical record from a past phase of the project.**
It is **NOT active project context.** Superseded deliverables, planning docs, and diagrams land
here when they are replaced, so the active working surface (repo root) stays clean and small.

## Rules for agents (this is enforced, not advisory)

- **Do not read, grep, or load any file under `archive/` by default.** A `PreToolUse` hook
  (`.claude/hooks/archive-guard.py`) **denies** `Read` / `Grep` / `Glob` on `archive/**` unless the
  archive has been explicitly unlocked.
- Access these files **only** when a human has explicitly asked to compare to, or access, past-week
  work. Then: unlock (`/doc-archive` skill → `unlock`), do the comparison, and `lock` again when done.
- The current, authoritative documents live at the repo root and are indexed in
  `.claude/CLAUDE.md` under **"Document currency."** If it isn't under `archive/`, it's current.

## Lifecycle invariant

Superseding a document = **move the old version into `archive/week-N/` in the same change** that
introduces the new one. Never leave two versions on the active surface.

## Known limitations (context hygiene, not access control)

- The guard covers `Read`/`Grep`/`Glob`. A `Bash` `cat`, or a repo-wide `Grep` whose *path* isn't
  under `archive/`, can still surface archived content — but results are always path-labeled
  `archive/...`, so their historical status is visible. The goal is preventing *accidental* whole-file
  reads and context bloat, and making historical status unmistakable — not locking out a determined agent.

## Layout

- `week-N/` — documents superseded during or before week N.
  - `week-1/` — the Week 1 solution-ideation + architecture set (`ARCHITECTURE.md`,
    `architecture.mmd`, `technical-decisions.md`, `MVP_BUILD_PLAN.md`) and `planning/` (built-feature
    implementation plans: temporal, CDS, write-back, production-grade).
