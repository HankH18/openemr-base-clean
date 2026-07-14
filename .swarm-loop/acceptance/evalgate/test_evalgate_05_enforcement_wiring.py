"""feat_evalgate criterion 5 (F10a) — enforcement wiring: a git hook invokes
the gate pre-push AND the existing `.gitlab-ci.yml` ``agent:tests`` job invokes
it. Enforcement lives in GitLab CI, NOT GitHub Actions (GitLab
branch-protection itself is a documented operator step, not asserted here).

FROZEN GOALS, deterministic file checks. Accepted pre-push wiring: a committed
pre-push hook file (e.g. .githooks/pre-push, scripts/hooks/pre-push) or a
pre-push stage in .pre-commit-config.yaml — either must reference the gate
(evals/gate.py or an eval-gate invocation). An installed .git/hooks/pre-push
also counts as evidence.
"""

from __future__ import annotations

import re
import subprocess

GATE_RE = re.compile(r"(evals?[/\\.](gate|run_gate|eval_gate))|eval[-_ ]?gate", re.I)
HOOK_CANDIDATES = (
    ".githooks/pre-push",
    "githooks/pre-push",
    "scripts/hooks/pre-push",
    "agent/scripts/hooks/pre-push",
    "agent/scripts/pre-push",
    ".hooks/pre-push",
    ".git/hooks/pre-push",
)


def test_evalgate_05_git_hook_and_gitlab_ci_invoke_gate(repo_root):
    problems: list[str] = []

    # --- pre-push git hook -------------------------------------------------
    hook_texts: list[tuple[str, str]] = []
    for rel in HOOK_CANDIDATES:
        path = repo_root / rel
        if path.is_file():
            hook_texts.append((rel, path.read_text(errors="ignore")))
    try:  # sweep tracked files for any other committed pre-push hook location
        tracked = subprocess.run(
            ["git", "ls-files", "*pre-push*"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        seen = {rel for rel, _ in hook_texts}
        for rel in tracked.stdout.splitlines():
            rel = rel.strip()
            path = repo_root / rel
            if rel and rel not in seen and path.is_file():
                hook_texts.append((rel, path.read_text(errors="ignore")))
    except (OSError, subprocess.SubprocessError):
        pass
    pre_commit_cfg = repo_root / ".pre-commit-config.yaml"
    if pre_commit_cfg.is_file():
        text = pre_commit_cfg.read_text(errors="ignore")
        if "pre-push" in text:
            hook_texts.append((".pre-commit-config.yaml", text))

    if not any(GATE_RE.search(text) for _, text in hook_texts):
        problems.append(
            "no pre-push git hook invokes the eval gate — expected a committed "
            "pre-push hook (e.g. .githooks/pre-push) or a pre-push stage in "
            ".pre-commit-config.yaml referencing evals/gate; "
            f"checked: {[rel for rel, _ in hook_texts] or 'no candidates found'}"
        )

    # --- GitLab CI: the agent:tests job ------------------------------------
    ci_path = repo_root / ".gitlab-ci.yml"
    if not ci_path.is_file():
        problems.append(".gitlab-ci.yml is missing from the repo root")
    else:
        lines = ci_path.read_text().splitlines()
        start = next(
            (i for i, ln in enumerate(lines) if ln.rstrip() == "agent:tests:"), None
        )
        if start is None:
            problems.append(".gitlab-ci.yml must keep the existing agent:tests job")
        else:
            block: list[str] = []
            for ln in lines[start + 1 :]:
                if ln and not ln[0].isspace():
                    break  # next top-level key
                block.append(ln)
            if not GATE_RE.search("\n".join(block)):
                problems.append(
                    "the .gitlab-ci.yml agent:tests job must invoke the eval "
                    "gate (evals/gate.py) so a >5% regression fails CI"
                )

    # --- NOT GitHub Actions -------------------------------------------------
    workflows = repo_root / ".github" / "workflows"
    if workflows.is_dir():
        offenders = [
            p.name
            for p in sorted(workflows.glob("*.y*ml"))
            if GATE_RE.search(p.read_text(errors="ignore"))
        ]
        if offenders:
            problems.append(
                "eval-gate enforcement must live in GitLab CI, NOT GitHub "
                f"Actions; found gate references in workflows: {offenders}"
            )

    assert not problems, "enforcement wiring incomplete:\n- " + "\n- ".join(problems)
