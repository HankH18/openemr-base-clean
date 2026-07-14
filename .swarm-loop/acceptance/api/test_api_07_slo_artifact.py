"""feat_api criterion 7 — SLO/alerts artifact (structural): a stubbed run emits
a latency-report artifact with numeric p50/p95 for doc-ingestion and
evidence-retrieval, and OBSERVABILITY.md carries SLO definitions + Week-2 alert
definitions with response actions (required-section check). No pass/fail
judgement on the p95 values themselves.

FROZEN GOALS. Contract pinned here: the report script lives at
``agent/scripts/latency_report.py`` (``slo_report.py`` / ``latency_slo_report.py``
also accepted), accepts ``--out PATH``, runs stubbed/LLM-free (the harness
strips API keys), exits 0, and writes JSON in which a dict node with numeric
``p50``+``p95`` is reachable under a key path mentioning ingestion, and another
under a key path mentioning retrieval.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_DIR = REPO_ROOT / "agent"
SCRIPT_CANDIDATES = ("latency_report.py", "slo_report.py", "latency_slo_report.py")


def _walk(node, path="$"):
    yield path, node
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def test_api_07_latency_artifact_and_observability_sections(tmp_path):
    script = next(
        (
            AGENT_DIR / "scripts" / name
            for name in SCRIPT_CANDIDATES
            if (AGENT_DIR / "scripts" / name).is_file()
        ),
        None,
    )
    if script is None:
        pytest.fail(
            "SLO latency-report script missing: expected "
            "agent/scripts/latency_report.py (accepts --out PATH; stubbed, "
            "LLM-free) emitting numeric p50/p95 for doc-ingestion and "
            "evidence-retrieval"
        )

    out = tmp_path / "latency_report.json"
    env = dict(os.environ)
    env.update(
        {
            "COPILOT_ANTHROPIC_API_KEY": "",
            "COPILOT_VOYAGE_API_KEY": "",
            "COPILOT_COHERE_API_KEY": "",
            "COPILOT_LANGFUSE_HOST": "",
            "COPILOT_LANGFUSE_PUBLIC_KEY": "",
            "COPILOT_LANGFUSE_SECRET_KEY": "",
        }
    )
    proc = subprocess.run(
        [sys.executable, str(script), "--out", str(out)],
        cwd=str(AGENT_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"{script.name} --out <path> must complete a stubbed, LLM-free run and "
        f"exit 0; rc={proc.returncode}\nstderr: {proc.stderr[-400:]}"
    )
    assert out.is_file(), "the latency-report artifact must be written to --out"
    report = json.loads(out.read_text())

    nodes = [
        (p, n)
        for p, n in _walk(report)
        if isinstance(n, dict) and _is_num(n.get("p50")) and _is_num(n.get("p95"))
    ]
    ingest = [p for p, _ in nodes if re.search(r"ingest", p, re.I)]
    retrieve = [p for p, _ in nodes if re.search(r"retriev", p, re.I)]
    assert ingest, (
        "the artifact must carry numeric p50/p95 for DOCUMENT INGESTION; "
        f"p50/p95 nodes found at: {[p for p, _ in nodes] or 'none'}"
    )
    assert retrieve, (
        "the artifact must carry numeric p50/p95 for EVIDENCE RETRIEVAL; "
        f"p50/p95 nodes found at: {[p for p, _ in nodes] or 'none'}"
    )

    # Required-section check over OBSERVABILITY.md.
    obs_path = REPO_ROOT / "OBSERVABILITY.md"
    assert obs_path.is_file(), "OBSERVABILITY.md must exist at the repo root"
    obs = obs_path.read_text()
    missing: list[str] = []
    if not re.search(r"\bSLOs?\b", obs):
        missing.append("an SLO definitions section")
    if "p95" not in obs:
        missing.append("p95 targets")
    if not re.search(r"ingest", obs, re.I):
        missing.append("a doc-ingestion SLO")
    if not re.search(r"retriev", obs, re.I):
        missing.append("an evidence-retrieval SLO")
    if not re.search(r"week\s*-?\s*2", obs, re.I):
        missing.append("Week-2 alert definitions (a Week-2-labelled section)")
    if not re.search(r"alert", obs, re.I):
        missing.append("alert definitions")
    if not re.search(r"(response|action|on-call|runbook)", obs, re.I):
        missing.append("response actions for each alert")
    assert not missing, (
        "OBSERVABILITY.md is missing required Week-2 sections: " + ", ".join(missing)
    )
