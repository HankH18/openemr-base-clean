"""Shared helpers for the frozen feat_evalgate acceptance suite (Week 2, F10).

FROZEN GOAL HARNESS — do not edit to make a test pass. These criteria are
deterministic file- and subprocess-checks over the BLOCKING tier of the
two-tier eval gate: a stubbed, LLM-free runner over the golden set, a
committed baseline with >5% relative-regression blocking, a fault-injection
self-proof, and pre-push + GitLab-CI enforcement wiring.

Gate contract pinned by this suite (implementers build against it):

- **Entry point:** ``agent/evals/gate.py`` (``run_gate.py`` / ``eval_gate.py``
  are also accepted). Runs stubbed/LLM-free — this harness strips API keys
  from the subprocess env.
- ``--out PATH`` writes machine-readable results JSON:
  ``{"pass_rate": <0..100 number>, "cases": [{..., "schema_valid": bool,
  "citation_present": bool, "factually_consistent": bool,
  "safe_refusal": bool, "no_phi_in_logs": bool}, ...]}``
  (a bare top-level list of case objects is also accepted, but then the
  overall ``pass_rate`` must still be discoverable — prefer the object form).
- ``--baseline PATH`` points the comparison at an alternate baseline JSON
  (which must at least carry ``pass_rate``); without the flag the COMMITTED
  baseline artifact is used.
- ``--inject-regression`` deterministically flips a known subset of case
  outcomes (fault injection) so the gate can prove it trips.
- **Exit codes:** 0 = no blocking regression; nonzero = regression detected.
  Exit 2 / "unrecognized arguments" is treated as "CLI contract not
  implemented" and fails the criterion explicitly.

- **Golden set:** JSONL case files under ``agent/evals/`` (any depth, filename
  not containing "result"); each golden case declares its target rubric via a
  ``rubric`` string, a ``rubrics`` list, or a ``category`` equal to one of the
  five rubric names.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

RUBRICS = (
    "schema_valid",
    "citation_present",
    "factually_consistent",
    "safe_refusal",
    "no_phi_in_logs",
)

_HERE = Path(__file__).resolve()
REPO_ROOT = _HERE.parents[3]
AGENT_DIR = REPO_ROOT / "agent"
EVALS_DIR = AGENT_DIR / "evals"
GATE_CANDIDATES = ("gate.py", "run_gate.py", "eval_gate.py")


def _stub_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "COPILOT_ANTHROPIC_API_KEY": "",
            "COPILOT_VOYAGE_API_KEY": "",
            "COPILOT_COHERE_API_KEY": "",
            "COPILOT_LANGFUSE_HOST": "",
            "COPILOT_LANGFUSE_PUBLIC_KEY": "",
            "COPILOT_LANGFUSE_SECRET_KEY": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


@pytest.fixture
def rubrics() -> tuple[str, ...]:
    return RUBRICS


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def evals_dir() -> Path:
    return EVALS_DIR


@pytest.fixture
def require_gate():
    """Resolve the gate entry point, failing (ran-and-failed) when absent."""

    def _find() -> Path:
        for name in GATE_CANDIDATES:
            path = EVALS_DIR / name
            if path.is_file():
                return path
        pytest.fail(
            "eval GATE runner missing: expected agent/evals/gate.py (or "
            "run_gate.py / eval_gate.py) — the blocking, stubbed, LLM-free "
            "tier of the two-tier eval gate (CLI contract in the feat_evalgate "
            "conftest docstring)"
        )

    return _find


@pytest.fixture
def run_gate(require_gate):
    """Run the gate as a subprocess under the agent venv, keys stripped."""

    def _run(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
        gate = require_gate()
        proc = subprocess.run(
            [sys.executable, str(gate), *args],
            cwd=str(AGENT_DIR),
            env=_stub_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode == 2 or "unrecognized arguments" in proc.stderr:
            pytest.fail(
                f"gate CLI contract not implemented for args {list(args)!r} "
                f"(usage error, rc={proc.returncode}).\n"
                f"stderr tail: {proc.stderr[-400:]}"
            )
        return proc

    return _run


@pytest.fixture
def load_results():
    """Parse a gate --out results file -> (cases, pass_rate)."""

    def _load(path: Path):
        assert path.is_file(), f"the gate did not write its --out results file at {path}"
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data, None
        if isinstance(data, dict):
            cases = data.get("cases") or data.get("results") or []
            pass_rate = data.get("pass_rate", data.get("overall_pass_rate"))
            return cases, pass_rate
        pytest.fail(
            f"gate results must be a JSON object (or list of cases); got {type(data).__name__}"
        )

    return _load


@pytest.fixture
def golden_cases():
    """Collect every eval case that declares a Week-2 rubric target.

    Scans agent/evals/**/*.jsonl (skipping files with "result" in the name).
    Returns a list of (path, case_object, [targeted rubrics]).
    """

    def _collect() -> list[tuple[Path, dict, list[str]]]:
        found: list[tuple[Path, dict, list[str]]] = []
        for path in sorted(EVALS_DIR.rglob("*.jsonl")):
            if "result" in path.name.lower():
                continue
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                declared = obj.get("rubrics")
                if not isinstance(declared, list):
                    declared = [obj.get("rubric") or obj.get("category")]
                targets = [r for r in declared if r in RUBRICS]
                if targets:
                    found.append((path, obj, targets))
        return found

    return _collect
