"""R4 bite-proofs: the legibility floor is calibrated to real OCR, and the
winning-span bbox is length-validated before it is emitted.

Two independent defects, one file.

P2 (calibration) — the R3 decouple left ``doc_extraction_confidence_threshold``
defaulting to 0.5 with a comment claiming that sits *below* the real-OCR-noise
band (~0.53-0.55). That rationale is EMPIRICALLY FALSE. Measured: real
Tesseract@200dpi on ``demo/sample_docs/sample_lab_report.pdf`` reads CORRECT
numeric lab values at very low confidence — ``"15.8"`` at min_conf 0.03,
``"4.24"`` at 0.08, ``"1.88"`` at 0.27 — because numeric/glyph tokens score
roughly 0.03-0.44 even when read exactly right. A 0.5 floor therefore strips the
bbox citation from correctly-read lab numbers, degrading the very evidence the
reconciliation gate exists to produce. OCR confidence is NOT a reliable
legibility signal; two-sided coverage + similarity are the real,
confidence-independent proof that a value is on the page. The fix lowers the
deployed floor to a small minimal guard that admits every correctly-read value
while still withholding a token OCR marked with LITERAL ZERO confidence.

These P2 tests read the REAL deployed floor (``Settings().
doc_extraction_confidence_threshold``), so they are RED at the old 0.5 default and
GREEN once it is lowered — the calibration is what they prove, at the real config.

P3 (defensive bbox) — ``reconcile_value`` reads a bbox only for the *winning*
span, and until now emitted it unvalidated. A winning token whose bbox is missing,
non-numeric, or not exactly ``[x, y, w, h]`` either produced a corrupt <4-element
bbox (single-token span, returned verbatim by ``_union_bbox``) or raised
``IndexError`` (multi-token span, ``_union_bbox`` indexing ``box[2]/box[3]``). The
fix validates the winning span's boxes (``len == 4``, numeric) and fails CLOSED —
unsupported, no bbox — mirroring ``_page_layout``'s defensive ``None`` return. A
located-but-unciteable value is surfaced as unverified, the safe direction for a
no-invention gate; a corrupt box that ``EvidenceOverlay.tsx`` cannot draw, or a
crash mid-pipeline, is not.
"""

from __future__ import annotations

import pytest

from copilot.config import Settings
from copilot.documents.reconcile import reconcile_value


def _one_token(text: str, conf: float) -> list[dict[str, object]]:
    """A single verbatim OCR token printing ``text`` at OCR confidence ``conf``."""
    return [{"text": text, "bbox": [0.20, 0.30, 0.08, 0.03], "conf": conf}]


def _deployed_floor() -> float:
    """The REAL deployed legibility floor the pipeline passes to reconcile_value."""
    return Settings().doc_extraction_confidence_threshold


# --- P2: the deployed floor admits correctly-read low-confidence lab numbers ---


class TestDeployedFloorAdmitsRealOcrLabNumbers:
    """Correctly-read numeric values keep their citation at the REAL deployed floor.

    RED at the old 0.5 default (every value below it is stripped of its bbox);
    GREEN once the floor is calibrated below the measured real-token band.
    """

    def test_synthesized_low_conf_located_value_keeps_citation(self) -> None:
        """A fully-located value read at min_conf ~0.05 stays supported with a bbox.

        The headline P2 case, stated at the deployed configuration: coverage 1.0,
        similarity 1.0, a single glyph read faintly at 0.05. At the old 0.5 floor
        this returned unsupported (0.05 < 0.5) — the citation stripped from a
        correctly-read value. Below the measured band it is supported.
        """
        result = reconcile_value("4.24", _one_token("4.24", 0.05), threshold=_deployed_floor())

        assert result.supported is True, (
            "a value read verbatim (coverage/similarity 1.0) but at low OCR "
            "confidence lost its bbox citation — the P2 legibility-floor defect"
        )
        assert result.bbox is not None

    @pytest.mark.parametrize(
        ("value", "min_conf"),
        [
            ("15.8", 0.03),  # measured: real Tesseract read this correct value at 0.03
            ("4.24", 0.08),  # measured: 0.08
            ("1.88", 0.27),  # measured: 0.27
        ],
    )
    def test_measured_tesseract_lab_values_keep_citation(self, value: str, min_conf: float) -> None:
        """The three CORRECT values measured off the demo lab report stay grounded.

        These are not hypotheticals: real Tesseract@200dpi on the demo lab report
        read each of these correct numeric values at exactly this (very low)
        confidence. At the old 0.5 floor all three lost their bbox; at the
        calibrated floor all three keep it.
        """
        result = reconcile_value(value, _one_token(value, min_conf), threshold=_deployed_floor())

        assert result.supported is True, (
            f"correct value {value!r} read at min_conf {min_conf} was stripped of "
            "its citation by an over-high legibility floor"
        )
        assert result.bbox is not None


# --- P2 guards: the calibration does not soften location, and keeps a guard ----


class TestCalibrationKeepsTheConfidenceIndependentGates:
    """Lowering the floor must not readmit invented values or drop the guard."""

    def test_shrink_class_stays_rejected_at_the_deployed_floor(self) -> None:
        """``18`` vs printed ``180`` stays unsupported — coverage is confidence-independent.

        The R1 shrink defect: ``18`` is a subsequence of ``180`` (span-side coverage
        2/3 = 0.667). Two-sided coverage rejects it regardless of the legibility
        floor, so lowering the floor cannot readmit it. Proves the P2 calibration
        touched only legibility, never location.
        """
        result = reconcile_value("18", _one_token("180", 0.97), threshold=_deployed_floor())

        assert result.supported is False, "the confidence-independent shrink guard must hold"
        assert result.bbox is None

    def test_literal_zero_confidence_read_is_still_withheld_by_the_minimal_guard(self) -> None:
        """A token OCR marked with LITERAL zero confidence is still withheld.

        The floor is calibrated to a small positive minimal guard, not disabled.
        Its one remaining job: withhold a token whose OCR confidence is literally
        0.0 — OCR's own "I could not read this" — even when the characters line up.
        This is the ``> 0`` floor asserted below; drop it to 0.0 and this token
        would be supported (coverage would carry it), which is a deliberate,
        documented alternative, not this deployment's choice.
        """
        floor = _deployed_floor()
        assert floor > 0.0, "the deployed floor is a positive minimal guard"

        result = reconcile_value("4.24", _one_token("4.24", 0.0), threshold=floor)

        assert result.supported is False
        assert result.bbox is None
        assert result.match_confidence == 0.0


# --- P3: the winning-span bbox is length-validated before it is emitted --------


class TestWinningSpanBboxIsLengthValidated:
    """A malformed or missing winning-span bbox fails closed — no corrupt box, no crash."""

    def test_single_token_short_bbox_is_unsupported_not_a_corrupt_box(self) -> None:
        """A winning single token with a 2-element bbox yields unsupported, not [x, y].

        Before the fix ``_union_bbox`` returned the 2-element box verbatim (its
        single-token branch), so the value was certified ``supported=True`` with a
        bbox that is not ``[x, y, w, h]`` — a box the overlay cannot draw. Now it
        fails closed.
        """
        tokens = [{"text": "4.24", "bbox": [0.20, 0.30], "conf": 0.97}]

        result = reconcile_value("4.24", tokens, threshold=0.0)

        assert result.supported is False
        assert result.bbox is None

    def test_multi_token_short_bbox_does_not_crash_and_is_unsupported(self) -> None:
        """A winning multi-token span with a 3-element bbox must not raise IndexError.

        The value ``sulfa drug`` wins the two-token span; the second token's bbox is
        ``[x, y, w]`` (no height). Before the fix ``_union_bbox`` indexed ``box[3]``
        and raised ``IndexError`` mid-pipeline. Now the malformed span fails closed.
        """
        tokens = [
            {"text": "sulfa", "bbox": [0.20, 0.30, 0.05, 0.02], "conf": 0.90},
            {"text": "drug", "bbox": [0.26, 0.30, 0.05], "conf": 0.90},  # len 3: no height
        ]

        result = reconcile_value("sulfa drug", tokens, threshold=0.0)

        assert result.supported is False
        assert result.bbox is None

    def test_missing_bbox_key_on_winning_token_fails_closed(self) -> None:
        """A winning token with neither ``bbox`` nor ``box`` fails closed, not raises.

        ``_token_field`` raises ``KeyError`` for such a token; before the fix that
        propagated out of ``reconcile_value``. The winning-span read now catches it
        and returns unsupported.
        """
        tokens = [{"text": "4.24", "conf": 0.97}]

        result = reconcile_value("4.24", tokens, threshold=0.0)

        assert result.supported is False
        assert result.bbox is None

    def test_wellformed_winning_bbox_is_still_supported(self) -> None:
        """Guard against over-rejection: a valid 4-element bbox is unchanged.

        The validation must reject only malformed boxes, never a legitimate one.
        """
        tokens = [{"text": "4.24", "bbox": [0.20, 0.30, 0.08, 0.03], "conf": 0.97}]

        result = reconcile_value("4.24", tokens, threshold=0.0)

        assert result.supported is True
        assert result.bbox == [0.20, 0.30, 0.08, 0.03]
