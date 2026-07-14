"""feat_api criterion 3 — evidence separation: the extended chat response
carries guideline evidence as a SEPARATE labeled block, never mixed into
patient-fact claims.

FROZEN GOALS, black-box over HTTP. Contract pinned here: the chat body exposes
a top-level ``guideline_evidence`` (or ``evidence``) list; each entry is typed
as guideline evidence (``source_type == "guideline"`` and/or ``chunk_id`` +
``section``); no patient-fact claim's citation is of guideline type.
"""

from __future__ import annotations

CLINICIAN_ID = 9001
EVIDENCE_KEYS = ("guideline_evidence", "evidence")


def test_api_03_guideline_evidence_separate_block(client, start_rounds):
    start_rounds(client, [1002])
    r = client.post(
        "/v1/chat",
        json={
            "clinician_id": CLINICIAN_ID,
            "patient_id": 1002,
            "message": "Per guidelines, does this potassium of 5.6 need treatment?",
        },
    )
    assert r.status_code == 200, f"POST /v1/chat -> {r.status_code}: {r.text[:300]}"
    body = r.json()

    key = next((k for k in EVIDENCE_KEYS if k in body), None)
    assert key is not None, (
        "the chat response must carry guideline evidence as a separate labeled "
        f"block (one of {EVIDENCE_KEYS}, a list; empty when retrieval finds "
        f"nothing) — got keys {sorted(body)}"
    )
    block = body[key]
    assert isinstance(block, list), (
        f"{key!r} must be a list of guideline-evidence entries; got {type(block).__name__}"
    )
    for item in block:
        assert isinstance(item, dict), f"evidence entries must be objects; got {item!r}"
        typed_as_guideline = (
            item.get("source_type") == "guideline"
            or "chunk_id" in item
            or "section" in item
        )
        assert typed_as_guideline, (
            "every evidence entry must be explicitly typed as guideline evidence "
            f"(source_type='guideline' and/or chunk_id/section); got keys {sorted(item)}"
        )

    # Never mixed: patient-fact claims must not carry guideline citations.
    for claim in body.get("claims", []):
        source_ref = claim.get("source_ref") or {}
        assert source_ref.get("source_type") != "guideline", (
            "guideline citations must never appear inside patient-fact claims — "
            "guideline backing belongs exclusively in the separate evidence block"
        )
