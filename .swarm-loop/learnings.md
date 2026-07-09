# Process-loop learnings — AgentForge run

Append imperative, specific lessons about *the orchestration* (task sizing, packet
gaps, merge collisions, wave ordering). Injected into task packets. Distilled into
the skill's LEARNED.md at termination.

## Cycle 1

### Wave 1 boundary (4 parallel enablers)
- Disjoint file-ownership mapping produced ZERO merge conflicts across 4 parallel tasks.
  Keep pre-dispatch ownership mapping strict; it is the single highest-leverage step.
- Subagents defensively bypass git hooks (`--no-verify` / `core.hooksPath=/dev/null`) citing
  the OpenEMR PHP-container commit hook from CLAUDE.md. NO such hook is actually active for the
  Python `agent/` subtree (only sample hooks; no `core.hooksPath`). Harmless, and `.git/config`
  was NOT persistently mutated — but future packets can state "no git hook applies here; commit
  normally" to stop the noise. Always confirm `.git/config` hooksPath after a wave.
- Sequential `git merge --no-ff` in a tight shell loop hit a transient `fatal: stash failed` on
  the 3rd merge; the tree was clean and simply re-running the merge succeeded. Treat a lone
  "stash failed" as transient (retry), not as a content conflict.
- Pattern worth repeating: when a producer must satisfy a downstream deterministic gate, have it
  reuse the gate's own extraction (StubAgent used verification's `extract_field_value` to fill
  claim source_ref values → claims are guaranteed to pass value-match).
- E4 had to change `probe_factories or default` → `if probe_factories is None` so an explicit
  empty list disables probes; watch for truthiness bugs when a packet requires "empty means empty".

### Wave 2 / integration boundary
- A shared auto-registration/plugin mechanism written by an early task (E4's `register_routers`)
  had an idempotency test whose premise ("no route modules exist") went false the moment the
  FIRST route module landed (R1). FastAPI `include_router` is not idempotent. The route task
  correctly refused to edit the out-of-ownership test and reported it under NEEDS; the orchestrator
  fixed the mechanism centrally (guard against re-mounting). LESSON: when an enabler ships a
  "registry" + a test asserting a property that only holds while the registry is EMPTY, expect it
  to break on the first consumer — either build the property for real up front (idempotency) or
  don't assert the empty-state invariant. Flag such tests during enabler review.
- Letting a feature task RUN (not edit) the frozen acceptance slice for its feature as its
  self-check target worked extremely well — R1 hit 6/6 before returning. Keep giving feature
  packets their frozen acceptance command as the done-condition.
