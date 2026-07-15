"""Recorded fixtures for the deterministic, keyless document-ingestion stubs.

Both keyless collaborators replay these recordings so the whole pipeline runs
green offline (no tesseract binary, no Anthropic key):

- ``StubOcr`` replays :data:`STUB_PAGE_TOKENS` as a page's word boxes.
- ``StubVision`` replays :data:`STUB_LAB_FACTS` / :data:`STUB_INTAKE_FACTS` as a
  structured extraction.

They are recorded *together* — the stub-vision value (``"13.5"``) is a token in
the stub-OCR page — so reconciliation locates the value on the page exactly as
it would for a real OCR/vision pair. Editing one without the other would break
that no-invention property, so they live side by side here on purpose.
"""

from __future__ import annotations

from typing import Any

# Word-level OCR tokens for a recorded lab page. Normalized ``[x, y, w, h]``
# bboxes in [0, 1]; confidences in [0, 1]. The "13.5" token is what the recorded
# lab extraction below reconciles against.
STUB_PAGE_TOKENS: list[dict[str, Any]] = [
    {"text": "Hemoglobin", "bbox": [0.10, 0.10, 0.20, 0.03], "conf": 0.98},
    {"text": "13.5", "bbox": [0.32, 0.10, 0.06, 0.03], "conf": 0.97},
    {"text": "g/dL", "bbox": [0.40, 0.10, 0.06, 0.03], "conf": 0.96},
    # Drug-name tokens for the recorded medication-list extraction below. Additive
    # only — the lab/intake stubs above still reconcile against their own tokens.
    {"text": "Lisinopril", "bbox": [0.10, 0.20, 0.18, 0.03], "conf": 0.98},
    {"text": "Metformin", "bbox": [0.10, 0.25, 0.16, 0.03], "conf": 0.97},
]

# Recorded lab-report extraction — one fact whose verbatim value is present in
# STUB_PAGE_TOKENS, so it reconciles to a bbox with high match confidence.
STUB_LAB_FACTS: list[dict[str, Any]] = [
    {"field_path": "hemoglobin", "value": "13.5", "unit": "g/dL", "page_no": 1},
]

# Recorded intake-form extraction (kept consistent with the OCR page so it, too,
# reconciles). Each intake fact carries its OpenEMR ``category`` (IntakeFact). The
# intake path is not exercised by the acceptance suite but must behave the same way.
STUB_INTAKE_FACTS: list[dict[str, Any]] = [
    {"field_path": "chief_complaint", "value": "Hemoglobin", "page_no": 1, "category": "chief_complaint"},
]

# Recorded medication-list extraction — one IntakeFact per medication, each
# verbatim drug name present in STUB_PAGE_TOKENS so it reconciles to a bbox. Each
# carries ``category='medication'``; MedicationListDocument's validator drops any
# non-medication fact, so the persisted list stays homogeneous.
STUB_MEDLIST_FACTS: list[dict[str, Any]] = [
    {"field_path": "medications[0].name", "value": "Lisinopril", "page_no": 1, "category": "medication"},
    {"field_path": "medications[1].name", "value": "Metformin", "page_no": 1, "category": "medication"},
]
