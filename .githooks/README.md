# Git hooks

Committed git hooks for this repo. Currently:

- **`pre-push`** — runs the LLM-free eval gate (`agent/evals/gate.py`) and blocks
  the push on a >5% relative regression in the eval pass-rate vs the committed
  baseline (`agent/evals/gate_baseline.json`). This is the local mirror of the
  GitLab CI `agent:tests` enforcement.

## Activation

Git does not use these hooks until you point `core.hooksPath` at this directory
(one-time, per clone):

```bash
git config core.hooksPath .githooks
```

- Bypass once: `git push --no-verify`
- Deactivate: `git config --unset core.hooksPath`

Branch protection that requires the CI `agent:tests` job to pass before merge is
the server-side backstop and is configured in the GitLab project settings (an
operator step, not in this repo).
