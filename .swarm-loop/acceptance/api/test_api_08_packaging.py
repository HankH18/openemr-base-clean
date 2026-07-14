"""feat_api criterion 8 — packaging file-check (deterministic): the agent
Dockerfile installs tesseract; docker-compose.deploy.yml runs the agent DB on
pgvector/pgvector:pg16; the Caddy ingress raises the request-body cap to at
least the document upload limit (>= 10MB). The actual deploy is an operator
step — these are pure file checks.

FROZEN GOALS. The Caddy check inspects the committed ingress sources
(Caddyfile.example and Caddyfile.https.example — the live Caddyfile is
gitignored and copied from them).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIN_BODY_BYTES = 10 * 1024 * 1024  # the documented scanned-PDF upload limit

_UNIT = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
}


def _max_size_bytes(text: str) -> list[int]:
    sizes: list[int] = []
    for m in re.finditer(r"max_size\s+(\d+)\s*([A-Za-z]*)", text):
        unit = m.group(2).lower()
        if unit in _UNIT:
            sizes.append(int(m.group(1)) * _UNIT[unit])
    return sizes


def test_api_08_dockerfile_compose_caddy_packaging():
    problems: list[str] = []

    dockerfile = (REPO_ROOT / "agent" / "Dockerfile").read_text()
    if not re.search(r"tesseract", dockerfile, re.I):
        problems.append(
            "agent/Dockerfile must install the tesseract OCR system binary "
            "(e.g. apt-get install -y tesseract-ocr)"
        )

    compose = (REPO_ROOT / "docker-compose.deploy.yml").read_text()
    if "pgvector/pgvector:pg16" not in compose:
        problems.append(
            "docker-compose.deploy.yml must run the agent database on the "
            "pgvector/pgvector:pg16 image (vector extension available at deploy)"
        )

    for name in ("Caddyfile.example", "Caddyfile.https.example"):
        path = REPO_ROOT / name
        if not path.is_file():
            problems.append(f"{name} is missing from the repo root")
            continue
        text = path.read_text()
        sizes = _max_size_bytes(text)
        if "request_body" not in text or not sizes:
            problems.append(
                f"{name} must set a request_body {{ max_size ... }} — Caddy's "
                "default body cap rejects scanned-PDF uploads"
            )
        elif max(sizes) < MIN_BODY_BYTES:
            problems.append(
                f"{name} request_body max_size must be >= the upload limit "
                f"(>= 10MB); largest found: {max(sizes)} bytes"
            )

    assert not problems, "packaging file-checks failed:\n- " + "\n- ".join(problems)
