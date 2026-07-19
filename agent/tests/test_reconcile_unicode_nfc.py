"""P3 bite-proof: an accented value must reconcile regardless of Unicode form.

``_normalize`` did ``strip().lower()`` with no Unicode normalization. A composed
accented spelling (``"José"`` with U+00E9) and its decomposed twin (``"e"`` +
combining acute U+0301) are DIFFERENT character sequences, so a value extracted in
one form against an OCR token in the other never folds to the same string. The
two-sided coverage gate then refuses the pair and the value is flagged
``supported=False`` — it fails closed and drops a real citation, manufacturing an
"unsupported" verdict out of an encoding difference the page never contained.

The fix applies ``unicodedata.normalize("NFC", ...)`` before ``strip().lower()``.
Both forms fold to one NFC string, the span matches exactly, and support holds.
Reverting the normalize re-splits the two forms and reddens this test.
"""

from __future__ import annotations

import unicodedata

from copilot.documents.reconcile import reconcile_value


def _one_token(text: str) -> list[dict[str, object]]:
    """A single legible OCR token (conf 0.97) printing ``text`` verbatim."""
    return [{"text": text, "bbox": [0.20, 0.30, 0.08, 0.03], "conf": 0.97}]


def test_composed_value_reconciles_against_decomposed_token() -> None:
    value_nfc = unicodedata.normalize("NFC", "José")  # é as one codepoint (U+00E9)
    token_nfd = unicodedata.normalize("NFD", "José")  # e + combining acute (U+0301)
    # Precondition: the two spellings really are distinct byte sequences, so the
    # test exercises the normalization rather than a trivially-equal pair.
    assert value_nfc != token_nfd, "the two forms must differ for this test to mean anything"

    result = reconcile_value(value_nfc, _one_token(token_nfd), threshold=0.0)

    assert result.supported is True, (
        "a value on the page in a different Unicode form is still on the page — "
        "without NFC folding the gate fails closed and drops the citation"
    )
    assert result.bbox == [0.20, 0.30, 0.08, 0.03]


def test_decomposed_value_reconciles_against_composed_token() -> None:
    # Symmetric: the mismatch must fold whichever side carries which form.
    value_nfd = unicodedata.normalize("NFD", "Peña")
    token_nfc = unicodedata.normalize("NFC", "Peña")
    assert value_nfd != token_nfc

    result = reconcile_value(value_nfd, _one_token(token_nfc), threshold=0.0)

    assert result.supported is True
    assert result.bbox is not None


def test_a_genuinely_absent_accented_value_is_still_unsupported() -> None:
    # Guard the fix does not turn NFC folding into a blanket match: a different
    # accented word must still reconcile to nothing (the no-invention gate holds).
    result = reconcile_value(
        unicodedata.normalize("NFC", "José"),
        _one_token(unicodedata.normalize("NFC", "Ramírez")),
        threshold=0.0,
    )

    assert result.supported is False
