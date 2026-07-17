#!/usr/bin/env python3
"""Frozen metric: PHI occurrences in the captured logs/traces/eval-artifact corpus.

Prints a BARE NUMBER (the count of PHI occurrences) on the last stdout line with
exit 0 when the corpus is valid. Target is 0 — a de-identified pipeline must leak
no patient identifiers, raw document text, or extracted clinical values into its
captured logs, traces, or eval-run artifacts.

Exit-code contract (frozen with the goals):
- ``0`` + a bare number = a real measurement (0 when the corpus is clean OR empty).
- ``2`` = usage error (argparse).
- ``3`` = ENVIRONMENT ERROR, NO number, so env noise never reads as a regression —
  raised ONLY when the anti-vacuous SELF-PROOF fails (the scanner cannot flag known
  planted synthetic PHI → it must not be trusted to report 0) or a corpus file is
  unreadable.

Order of operations: the self-proof runs FIRST (the real anti-vacuous guard); then
the captured corpus is scanned and the leak count printed. An EMPTY corpus prints 0
honestly — the self-proof already proved the scanner works, and a built-but-silent
pipeline is caught by the feat_ingestion/feat_graph captured-artifact criteria, not
here. Corpus-incompleteness (missing event families) is a stderr WARNING, never a
hard fail, so ``measure`` is never aborted by a partial capture.

TODAY (pre-build) no capture corpus exists → self-proof passes, empty corpus → 0.
Once the Week-2 pipeline writes captured artifacts, this reports the real leak count.

Corpus discovery:
- ``PHI_SCAN_CORPUS`` (os.pathsep-separated files and/or dirs) overrides the roots.
- Otherwise the default capture roots under ``agent/`` are scanned recursively:
  ``artifacts/``, ``var/capture/``, ``logs/``. Only captured OUTPUT is in scope —
  the eval *input* golden set (which deliberately carries adversarial planted PHI)
  is NOT a corpus root.

Stdlib only for the scan itself (regex/JSON); no third-party imports are required,
so ``ensure_ready([])`` is the correct no-op self-sync probe (see _bootstrap).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import _bootstrap

AGENT = Path(__file__).resolve().parents[2] / "agent"

# Default capture roots (captured OUTPUT only — NOT the eval input golden set).
_DEFAULT_ROOTS = [
    AGENT / "artifacts",
    AGENT / "var" / "capture",
    AGENT / "logs",
]

# Text/data artifact extensions we treat as corpus. Source (.py) and rendered docs
# (.md) are deliberately excluded — the corpus is machine-captured run output.
_CORPUS_SUFFIXES = {".jsonl", ".ndjson", ".json", ".log", ".txt"}

# Dirs we never descend into even if they sit under a capture root.
_SKIP_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

# The event families a valid corpus MUST contain (the captured lifecycle). A corpus
# missing any of these is not a real end-to-end capture → env error, not a "0".
_EXPECTED_EVENT_FAMILIES = (
    "doc.ingest",
    "extraction.run",
    "guideline.retrieve",
    "worker.handoff",
    "verification.result",
)

# --- PHI detectors ----------------------------------------------------------
#
# Conservative, format-specific or label-gated so structured log noise
# (ISO timestamps, correlation IDs, latency/token counts) does NOT false-positive.
# A clean, de-identified corpus scores 0; any of these firing is a genuine leak.
_PHI_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Social Security Number — the 3-2-4 dashed shape (an ISO date is 4-2-2, no match).
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Medical record number — label-gated (a bare digit run is not enough).
    ("mrn", re.compile(r"(?i)\bMRN\b[:#]?\s*\d{3,}")),
    ("mrn", re.compile(r"(?i)\bmedical\s+record\s+(?:number|no\.?|#)\b[:#]?\s*\d{3,}")),
    # Phone — distinctive dashed or parenthesized shapes only (bare 10-digit runs,
    # which collide with epoch/latency numbers, are intentionally NOT matched).
    ("phone", re.compile(r"\(\d{3}\)\s*\d{3}[-.\s]?\d{4}\b")),
    ("phone", re.compile(r"\b\d{3}-\d{3}-\d{4}\b")),
    ("phone", re.compile(r"(?i)\b(?:phone|tel|mobile|cell|fax)\b[:\s]*\+?\d{1,2}?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
    # Email address.
    ("email", re.compile(r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b")),
    # Date of birth — label-gated (an unlabelled date is not PHI on its own).
    ("dob", re.compile(r"(?i)\b(?:DOB|date\s+of\s+birth|birth\s?date|born)\b[:\s]*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")),
    ("dob", re.compile(r"(?i)\b(?:DOB|date\s+of\s+birth|birth\s?date|born)\b[:\s]*\d{4}-\d{2}-\d{2}")),
    # Patient/member name — label-gated "First [M.] Last".
    ("patient_name", re.compile(
        r"(?i)\b(?:patient|member)(?:[ _-]*name)?[\"']?\s*[:=]\s*[\"']?"
        r"[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+"
    )),
    # Street address.
    ("address", re.compile(
        r"(?i)\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}"
        r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Lane|Ln|Dr|Drive|Ct|Court)\b"
    )),
]

# --- Planted-PHI self-proof fixture -----------------------------------------
#
# Embedded so this script stays self-contained (the two wrapper files are the only
# files created). Every value below is CLEARLY FAKE / SYNTHETIC — reserved-range
# SSN (900-xx), fictional 555-01xx phone, ".invalid" TLD, "Public"/"Fake" names —
# and never a real person. If the scanner cannot flag THIS, it must not be trusted
# to report 0 on the real corpus.
_SELF_PROOF_FIXTURE = (
    '{"event": "doc.ingest", "level": "INFO", '
    '"patient_name": "Jane Q. Public", '
    '"mrn": "MRN: 000123456", '
    '"ssn": "999-00-1234", '
    '"dob": "DOB: 01/02/1980", '
    '"phone": "(555) 010-1234", '
    '"alt_phone": "555-010-4321", '
    '"email": "jane.public@example.invalid", '
    '"address": "123 Fake Street", '
    '"note": "SYNTHETIC TEST RECORD — NOT A REAL PATIENT"}\n'
)

# The self-proof must flag comfortably more than this many occurrences. Set well
# below the number of planted items so a small regex tweak does not spuriously
# fail the proof, yet well above 0 so a broken scanner is caught loudly.
_SELF_PROOF_MIN = 3


def _count_phi(text: str) -> int:
    """Total number of PHI occurrences across all detectors in ``text``."""
    return sum(len(pat.findall(text)) for _name, pat in _PHI_PATTERNS)


def _iter_corpus_files() -> list[Path]:
    """Resolve the corpus file set from ``PHI_SCAN_CORPUS`` or the default roots."""
    override = os.environ.get("PHI_SCAN_CORPUS", "").strip()
    if override:
        roots = [Path(p).expanduser() for p in override.split(os.pathsep) if p.strip()]
    else:
        roots = list(_DEFAULT_ROOTS)

    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in _CORPUS_SUFFIXES:
                files.append(path)
    return files


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        _bootstrap.env_error(f"could not read corpus file {path}: {exc}")
    return ""  # unreachable — env_error raises


def main() -> None:
    # No positional/optional args; argparse exits 2 on any misuse, per the contract.
    argparse.ArgumentParser(description="Scan the captured corpus for PHI.").parse_args()

    # Match the shared self-sync pattern. The scan is stdlib-only, so the correct
    # import-probe set is empty (a no-op that still exercises the shared path).
    _bootstrap.ensure_ready([])

    # --- Guard (c): sensitivity self-proof runs FIRST -----------------------
    proof_hits = _count_phi(_SELF_PROOF_FIXTURE)
    if proof_hits < _SELF_PROOF_MIN:
        _bootstrap.env_error(
            f"PHI scanner self-proof FAILED: flagged {proof_hits} of the planted "
            f"synthetic PHI items (expected >= {_SELF_PROOF_MIN}); refusing to report a count"
        )

    # --- Corpus scan --------------------------------------------------------
    # An EMPTY corpus scores 0 honestly: the self-proof above already guarantees
    # the scanner is functional, and pre-build (or a run that produced no output)
    # has nothing to leak. A pipeline that is built but emits no captured events
    # is caught by the feat_ingestion / feat_graph captured-artifact criteria, not
    # here — so 0 on an empty corpus is not a vacuous pass. This MUST always emit a
    # number once the self-proof passes, or the swarm-loop `measure` aborts the
    # whole cycle (a no-number metric command exits 1).
    files = _iter_corpus_files()
    if not files:
        print(0)
        return

    combined = "\n".join(_read_text(p) for p in files)

    # Corpus completeness is DIAGNOSTIC only (stderr warning, never an env-error):
    # a hard-fail here would abort `measure` on any partial capture. The self-proof
    # is the anti-vacuous guard; detecting leaks is this metric's actual job.
    missing = [fam for fam in _EXPECTED_EVENT_FAMILIES if fam not in combined]
    if missing:
        print(
            "phi_check: WARNING — corpus missing expected event families: "
            + ", ".join(missing),
            file=sys.stderr,
        )

    print(_count_phi(combined))


if __name__ == "__main__":
    main()
