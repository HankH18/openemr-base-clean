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
    """Recursively sort dict keys.  NESTED lists stay in order — order can be
    meaningful for FHIR entries (e.g. procedure_report_seq).

    The TOP-LEVEL resource list is a separate matter: a FHIR fetch is a *set* of
    resources whose server-returned order is not material content, so
    :func:`content_hash_for_resources` sorts it (by ``(resourceType, id)``)
    before hashing.  This function only canonicalizes each resource's interior."""
    if isinstance(value, Mapping):
        return {k: _canonical(value[k]) for k in sorted(value.keys())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def _resource_sort_key(res: Mapping[str, Any]) -> tuple[str, str]:
    """Stable ordering key for a resource: ``(resourceType, id)``.

    Coerced to ``str`` so a missing or non-string field never makes the sort
    raise on a cross-type comparison — JSON has no total order across types, but
    ``str`` does, and equal keys keep their input order (Python's sort is
    stable), so the digest depends on content alone."""
    return (str(res.get("resourceType")), str(res.get("id")))


def content_hash_for_resources(resources: Sequence[Mapping[str, Any]]) -> str:
    """SHA-256 hex digest of the *material* content of ``resources``.

    ``meta`` is stripped before hashing.  Otherwise a server-side no-op
    that only bumps ``meta.lastUpdated`` (or ``versionId``) would look
    like a real change and trigger a Claude call for nothing — defeating
    the "cost scales with change, not with polling" principle.

    Empty input hashes to a stable, non-empty digest so callers can
    always compare hashes.

    The canonicalized resource list is sorted by ``(resourceType, id)`` before
    hashing, so the *order* the server returns the resources in does not affect
    the digest — only their content does (making good on this module's stated
    "insertion order ... do not affect the digest" invariant).  Without it, the
    same unchanged set re-fetched in a different order would look "changed" and
    trigger a spurious, billable re-synthesis.  Nested-list order inside a
    resource remains significant (see :func:`_canonical`).
    """

    def strip_meta(res: Mapping[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in res.items() if k != "meta"}

    canonical = sorted(
        (_canonical(strip_meta(r)) for r in resources), key=_resource_sort_key
    )
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
