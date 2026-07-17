"""feat_ingestion criterion 1 — Settings W2 fields + pricing rows.

Settings exposes the Week-2 fields (Voyage/Cohere API keys, OCR options, the
document-grounding confidence threshold, and at least one boolean ingestion
feature flag) with sane types/defaults; the pricing table EXPLICITLY lists
voyage-3.5, rerank-v3.5 and the configured vision model at nonzero rates
(fallback pricing does not count as "resolved"). FROZEN GOAL HARNESS.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest


def _names_matching(names, predicate):
    return sorted(n for n in names if predicate(n))


def test_01_settings_w2_fields_and_pricing():
    from copilot.config import Settings

    names = {n.lower() for n in Settings.model_fields}

    voyage = _names_matching(names, lambda n: "voyage" in n and ("key" in n or "api" in n))
    cohere = _names_matching(names, lambda n: "cohere" in n and ("key" in n or "api" in n))
    ocr = _names_matching(names, lambda n: "ocr" in n)
    threshold = _names_matching(
        names,
        lambda n: ("threshold" in n and ("doc" in n or "confidence" in n or "extract" in n))
        or ("confidence" in n and ("doc" in n or "extract" in n)),
    )
    missing = [
        label
        for label, found in (
            ("a Voyage API-key field (name containing 'voyage')", voyage),
            ("a Cohere API-key field (name containing 'cohere')", cohere),
            ("OCR option field(s) (name containing 'ocr')", ocr),
            ("a document-confidence-threshold field", threshold),
        )
        if not found
    ]
    if missing:
        pytest.fail("Settings is missing W2 fields — F2 not implemented: " + "; ".join(missing))

    s = Settings(_env_file=None)

    # Keys default to empty strings (keyless CI) and are typed str.
    assert isinstance(getattr(s, voyage[0]), str)
    assert isinstance(getattr(s, cohere[0]), str)

    # Threshold: a float with a conservative default in (0, 1).
    tv = getattr(s, threshold[0])
    assert isinstance(tv, float), f"{threshold[0]} must be a float (got {type(tv).__name__})"
    assert 0.0 < tv < 1.0, f"{threshold[0]} default must be a conservative value in (0, 1)"

    # At least one boolean W2 ingestion/document feature flag.
    flags = [
        n
        for n in Settings.model_fields
        if isinstance(getattr(s, n), bool)
        and any(k in n.lower() for k in ("document", "doc_", "ingest", "extraction"))
    ]
    assert flags, "Settings must expose at least one boolean document-ingestion feature flag"

    # --- pricing: explicit nonzero rows, not the unknown-model fallback ---
    import copilot.observability.pricing as pricing

    tables = [v for v in vars(pricing).values() if isinstance(v, Mapping)]
    known = set()
    for t in tables:
        known |= {k for k in t.keys() if isinstance(k, str)}

    vision_fields = _names_matching(
        names, lambda n: "vision" in n or ("model" in n and "extract" in n)
    )
    vision_model = getattr(s, vision_fields[0]) if vision_fields else s.anthropic_model_synthesis

    for model in ("voyage-3.5", "rerank-v3.5", vision_model):
        assert model in known, (
            f"pricing table must explicitly list {model!r} — the unknown-model "
            "fallback is not a resolved rate"
        )
        assert pricing.cost_usd(model, 1_000_000, 0) > 0.0, f"{model!r} must cost > 0"
