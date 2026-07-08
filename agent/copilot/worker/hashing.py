"""Content hashing for change-gate hash-confirm.

We hash a **canonicalized** JSON dump of the pulled resource set so that
insertion order, whitespace, and dict-key order do not affect the digest.
The hash is what gates re-synthesis: if it moves, we call Claude; if not,
we skip and save a token spend + latency budget.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _canonical(value: Any) -> Any:
    """Recursively sort dict keys.  Lists stay in order — order can be
    meaningful for FHIR entries (e.g. procedure_report_seq)."""
    if isinstance(value, Mapping):
        return {k: _canonical(value[k]) for k in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def content_hash_for_resources(resources: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of the *material* content of ``resources``.

    ``meta`` is stripped before hashing.  Otherwise a server-side no-op
    that only bumps ``meta.lastUpdated`` (or ``versionId``) would look
    like a real change and trigger a Claude call for nothing — defeating
    the "cost scales with change, not with polling" principle.

    Empty input hashes to a stable, non-empty digest so callers can
    always compare hashes.
    """

    def strip_meta(res: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in res.items() if k != "meta"}

    canonical = [_canonical(strip_meta(r)) for r in resources]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
