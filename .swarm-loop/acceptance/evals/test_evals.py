"""feat_evals — a runnable eval dataset + a captured results artifact.

FROZEN GOALS (file/content checks). The doc wants an eval dataset covering
boundary/invariant/authorization cases plus a submitted results artifact. Baseline:
only an LLM-gated grounding eval exists (skipped without a key) and no results file —
these fail until a deterministic dataset + a committed results summary exist.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EVALS = ROOT / "agent" / "evals"


def _glob(patterns: list[str]) -> list[Path]:
    return [p for pat in patterns for p in EVALS.glob(pat) if p.is_file()]


def test_eval_dataset_exists_with_cases():
    datasets = _glob(["*dataset*.jsonl", "*dataset*.json", "*cases*.jsonl", "*cases*.json"])
    assert datasets, "an eval dataset file must exist under agent/evals/"
    total = 0
    for p in datasets:
        text = p.read_text()
        if p.suffix == ".jsonl":
            total += sum(1 for line in text.splitlines() if line.strip())
        else:
            try:
                data = json.loads(text)
                total += len(data) if isinstance(data, list) else len(data.get("cases", []))
            except (ValueError, AttributeError):
                pass
    assert total >= 5, f"the eval dataset must contain >=5 cases; found {total}"


def test_eval_results_artifact_exists():
    results = _glob(["*result*.json", "*result*.md", "*report*.json", "*report*.md"])
    assert results, "a captured eval-results artifact must exist under agent/evals/"
    joined = " ".join(p.read_text(errors="ignore").lower() for p in results)
    assert any(k in joined for k in ("pass", "passed", "score", "accuracy")), (
        "the eval results artifact must report a pass/score summary"
    )
