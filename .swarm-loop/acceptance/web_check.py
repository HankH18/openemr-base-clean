#!/usr/bin/env python3
"""Frozen metric: count of passing feat_frontend (F9) criteria.

Prints a BARE NUMBER (0-6) on the last stdout line with exit 0. The frontend
feature is measured here rather than in ``run.py`` because it needs node/vitest,
not pytest. The six criteria (see acceptance-criteria.md, feat_frontend):

  1. Build:            ``npm run build`` succeeds.
  2. Vitest infra:     vitest is configured and executes the unit suite.
  3. ProvenanceChip:   fhir/document/guideline variants render distinctly.
  4. Overlay geometry: normalized [x,y,w,h] -> SVG rect coords.
  5. Upload flow:      FileTrigger posts multipart to /v1/documents (mocked).
  6. Citation adapter: API Citation union -> UI model, fail-safe fallback.

Criteria 1-2 are measured from the build/run themselves; 3-6 map onto vitest
tests by keyword (each passes iff >= 1 matching test exists AND all matching
tests pass — mirroring "a criterion passes only if all its tests pass").

Exit-code contract (frozen with the goals):
- ``0`` + a bare number = a real measurement.
- ``2`` = usage error (argparse).
- ``3`` = ENVIRONMENT ERROR (no number) when the TOOLCHAIN cannot run at all:
  node/npm/npx absent, the web package missing, or ``npm ci`` (run only when
  ``node_modules`` is absent) failing for npm/network reasons. Env noise must
  never read as a regression.

A FAILED build or an unconfigured/absent vitest is NOT an env error — those are
legitimate feature states: the matching criteria simply fail and the count
reflects only what passes. TODAY vitest is not yet configured, so criteria 2-6
fail cleanly and the count reflects the build alone (no crash).

Needs node, not pip packages, so ``ensure_ready([])`` is the correct no-op
self-sync probe (see _bootstrap — it names web_check as the empty-list case).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import _bootstrap

AGENT = Path(__file__).resolve().parents[2] / "agent"
WEB = AGENT / "web"

_NPM_CI_TIMEOUT = 600
_BUILD_TIMEOUT = 600
_VITEST_TIMEOUT = 600

# Keyword map for criteria 3-6 (matched case-insensitively against each vitest
# test's full name). A criterion passes iff there is at least one matching test
# and every matching test passed.
_CRITERION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "provenance_chip": ("provenance", "provenancechip"),
    "overlay_geometry": ("overlay", "geometry", "bbox", "bounding"),
    "upload_flow": ("upload", "filetrigger"),
    "citation_adapter": ("citation", "adapter"),
}


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(WEB),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _flatten_assertions(report: dict[str, Any]) -> list[tuple[str, str]]:
    """(full-name, status) for every assertion in a Jest-compatible vitest report."""
    out: list[tuple[str, str]] = []
    for file_result in report.get("testResults", []) or []:
        for a in file_result.get("assertionResults", []) or []:
            name = a.get("fullName") or " ".join(
                [*(a.get("ancestorTitles") or []), a.get("title") or ""]
            )
            out.append((name.lower(), str(a.get("status", ""))))
    return out


def _keyword_criterion_passes(assertions: list[tuple[str, str]], keywords: tuple[str, ...]) -> bool:
    matching = [(n, s) for (n, s) in assertions if any(k in n for k in keywords)]
    return bool(matching) and all(s == "passed" for _n, s in matching)


def main() -> None:
    # No args; argparse exits 2 on any misuse, per the contract.
    argparse.ArgumentParser(description="Measure feat_frontend (F9) passing criteria.").parse_args()

    # Needs node, not pip packages — empty probe is the documented no-op self-sync.
    _bootstrap.ensure_ready([])

    if not (WEB / "package.json").is_file():
        _bootstrap.env_error(f"web package not found at {WEB}")
    if shutil.which("npm") is None or shutil.which("npx") is None:
        _bootstrap.env_error("npm/npx not found on PATH; cannot measure the frontend")

    # `npm ci` only when node_modules is absent; a failure here is an env error
    # (npm/network unavailable), not a criterion failure.
    if not (WEB / "node_modules").is_dir():
        try:
            ci = _run(["npm", "ci"], timeout=_NPM_CI_TIMEOUT)
        except FileNotFoundError:
            _bootstrap.env_error("npm not executable; cannot install web dependencies")
        except subprocess.TimeoutExpired:
            _bootstrap.env_error("npm ci timed out (network unavailable?)")
        if ci.returncode != 0:
            _bootstrap.env_error(f"npm ci failed (rc={ci.returncode}): {(ci.stderr or ci.stdout)[-400:]}")

    passing = 0

    # --- Criterion 1: build succeeds ---------------------------------------
    try:
        build = _run(["npm", "run", "build"], timeout=_BUILD_TIMEOUT)
        build_ok = build.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        build_ok = False
    if build_ok:
        passing += 1

    # --- Criteria 2-6: vitest ----------------------------------------------
    # Use the locally-installed vitest only (no network fetch). If vitest is not
    # configured/installed yet, criteria 2-6 simply fail — this is not an env error.
    vitest_bin = WEB / "node_modules" / ".bin" / "vitest"
    report: dict[str, Any] | None = None
    if vitest_bin.exists():
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
            out_path = Path(tf.name)
        try:
            vt = _run(
                [str(vitest_bin), "run", "--reporter=json", f"--outputFile={out_path}"],
                timeout=_VITEST_TIMEOUT,
            )
            raw = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
            if not raw.strip():
                raw = vt.stdout  # some versions emit JSON to stdout
            report = json.loads(raw) if raw.strip() else None
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            report = None
        finally:
            out_path.unlink(missing_ok=True)

    if report is not None:
        assertions = _flatten_assertions(report)
        total_tests = int(report.get("numTotalTests", len(assertions)) or 0)

        # Criterion 2: vitest is configured and actually executed the unit suite.
        if total_tests >= 1:
            passing += 1

        # Criteria 3-6: keyword-mapped test groups.
        for keywords in _CRITERION_KEYWORDS.values():
            if _keyword_criterion_passes(assertions, keywords):
                passing += 1

    print(passing)


if __name__ == "__main__":
    main()
