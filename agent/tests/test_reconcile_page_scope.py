"""The no-invention gate INVERTED on exactly the pages it exists to protect.

``pipeline._reconcile_facts`` searched ``tokens_by_page.get(page_no) or
tokens_by_page.get(1) or []``. **An empty list is falsy**, so a page whose OCR
yielded ZERO tokens fell through to PAGE 1's tokens — while the fact's own
``page_no`` was still passed to ``reconcile_value`` and stamped onto the citation
(``pipeline.py`` ``page_no=recon.page_no if recon.supported else fact.page_no``).

The trigger is precisely the case reconciliation exists for: a page the vision
model can read but tesseract cannot — handwriting, a photographed or angled page,
a low-contrast fax, a rotated scan, all routine on clinical intake. The fact most
needing "we could not verify this" instead got ``supported=True`` at a *high*
confidence, because it IS a genuine exact string match — just on the wrong page.
Neither guard downstream can catch that: the confidence threshold sees a real 0.97
match, and ``vision._check_page_numbers`` sees a page number that really was in the
batch. Only the *pairing* is a lie. The physician then gets page 3's image
(``api/routes/documents.py`` serves it by the CITED page) with a highlight drawn at
page 1's coordinates (``web/src/components/EvidenceOverlay.tsx``).

**Why nothing caught it.** ``_reconcile_facts`` had zero direct coverage, and
``StubOcr``'s default single-page fixture returns page 1's tokens for EVERY page
(``ocr.py``: ``index = page_no if 0 <= page_no < len(self._pages) else 0``), so in
stub-based tests ``tokens_by_page`` is ``{1: T, 2: T, 3: T}`` and "fall back to
page 1" is *indistinguishable from correct*. The real demo corpus has the same
blind spot for the same reason — all three documents are single-page, so page 1 IS
the only page. The bug was undetectable by construction.

**So every fixture here gives different pages DIFFERENT tokens, and one page
none at all.** That is the property that makes the defect visible.
"""

from __future__ import annotations

from typing import Any

import pytest

from copilot.config import Settings
from copilot.documents.pipeline import _reconcile_facts
from copilot.domain.documents import ExtractedFact


def _token(text: str, x: float, y: float, w: float = 0.07, h: float = 0.02) -> dict[str, Any]:
    """One OCR word box, legible (conf 0.97) — the defect is never a weak match."""
    return {"text": text, "bbox": [x, y, w, h], "conf": 0.97}


# Page 1 carries a header that *coincidentally* contains the string "10 mg" —
# "DOB: 10 mg" is contrived, but a real page-1 header holding a value that also
# appears elsewhere (a dose, a date, an ID) is not.
_PAGE_1 = [_token("DOB:", 0.10, 0.10), _token("10", 0.14, 0.10), _token("mg", 0.19, 0.10)]
# Page 2 is legible and says something else entirely.
_PAGE_2 = [_token("Allergies:", 0.10, 0.30), _token("Sulfa", 0.20, 0.30)]
# Page 3 is the handwritten one: the model reads it, tesseract returns NOTHING.
_PAGE_3_HANDWRITTEN: list[dict[str, Any]] = []

# Distinct tokens per page — the whole point. A single-page fixture cannot see this.
_PAGES = {1: _PAGE_1, 2: _PAGE_2, 3: _PAGE_3_HANDWRITTEN}


def _settings() -> Settings:
    return Settings(database_url="sqlite+aiosqlite:///:memory:")


def _reconcile_one(
    value: str, page_no: int | None, pages: dict[int, list[dict[str, Any]]] | None = None
) -> Any:
    fact = ExtractedFact(field_path="dose", value=value, page_no=page_no)
    (_fact, recon), = _reconcile_facts([fact], _PAGES if pages is None else pages, _settings())
    return recon


# --- the headline: an unreadable page must not borrow page 1's evidence -------


def test_fact_on_page_with_zero_ocr_tokens_is_unsupported_not_blessed_by_page_1() -> None:
    """A handwritten dose on page 3 that tesseract could not read is UNVERIFIED.

    Page 3's OCR is empty and page 1 coincidentally prints "10 mg". Before the
    fix this returned ``supported=True`` at ``match_confidence=0.97`` with page
    1's header bbox stamped under ``page_no=3`` — a citation pointing at a place
    the value is not. There is exactly one honest answer for a page that cannot
    be searched: we could not verify it.
    """
    recon = _reconcile_one("10 mg", page_no=3)

    assert recon.supported is False, (
        "a page whose OCR yielded zero tokens was reconciled against PAGE 1's "
        "tokens and blessed — the no-invention gate inverted on exactly the page "
        "(handwriting/fax/photo) it exists to protect"
    )
    assert recon.bbox is None, (
        f"bbox {recon.bbox} came from page 1's tokens but would be drawn on page "
        "3's image — a highlight where the value is not"
    )
    assert recon.match_confidence == 0.0


def test_unreadable_page_does_not_inherit_page_1_coordinates() -> None:
    """The bbox specifically must not be page 1's header box.

    Stated separately from ``supported`` because the bbox is what a physician
    actually SEES: ``api/routes/documents.py`` serves the page image keyed by the
    cited page, and ``EvidenceOverlay.tsx`` draws this box on it.
    """
    recon = _reconcile_one("10 mg", page_no=3)

    page_1_header_y = _PAGE_1[1]["bbox"][1]
    assert recon.bbox is None or recon.bbox[1] != page_1_header_y


# --- the regression guard: real support must still be earned -----------------


def test_fact_whose_value_is_on_its_own_page_is_supported_with_that_pages_bbox() -> None:
    """Page 2's value reconciles to page 2's tokens and page 2's box.

    Without this, "never support anything" would pass the headline test. The fix
    must narrow the search to the named page, not disable reconciliation.
    """
    recon = _reconcile_one("Sulfa", page_no=2)

    assert recon.supported is True
    assert recon.page_no == 2
    assert recon.bbox is not None
    assert recon.bbox[1] == pytest.approx(_PAGE_2[1]["bbox"][1]), (
        "supported fact must carry the bbox of the page it names"
    )


# --- no cross-page bleed in the other direction ------------------------------


def test_value_present_only_on_page_1_is_not_supported_when_the_fact_names_page_2() -> None:
    """A page-2 fact must not be verified against page 1's text.

    Page 2 is perfectly legible here — it simply does not contain "10 mg". The
    old fallback needed the named page's tokens to be *falsy* to bleed, so this
    guards the general rule (search only the named page) rather than only the
    empty-page trigger.
    """
    recon = _reconcile_one("10 mg", page_no=2)

    assert recon.supported is False
    assert recon.bbox is None


# --- the deliberate page_no=None decision ------------------------------------


def test_unnumbered_fact_on_a_multi_page_document_is_refused_not_guessed_at_page_1() -> None:
    """With no page_no and several pages, we do not guess — searching page 1 is a guess.

    This is the milder half of the same bug: a coincidental page-1 header match
    blessed as provenance for a fact that never said it came from page 1. The
    cost is a fact that IS on page 1 but forgot to say so now reads unverified.
    That is the correct direction for a no-invention gate: unverified is
    recoverable, a wrong citation is not.
    """
    recon = _reconcile_one("10 mg", page_no=None)

    assert recon.supported is False
    assert recon.bbox is None


def test_unnumbered_fact_on_a_single_page_document_is_still_reconciled() -> None:
    """One page means "page 1" is not a fallback — it is the only page by elimination.

    Refusing here would cost real, honest support for nothing: with a single page
    there is no ambiguity about which page a fact came from. Measured relevance:
    all three documents in the real demo corpus are single-page, so this branch —
    not the multi-page one — is what the live ``extraction_field_pass_rate``
    actually rides on.
    """
    recon = _reconcile_one("10 mg", page_no=None, pages={1: _PAGE_1})

    assert recon.supported is True
    assert recon.page_no == 1
    assert recon.bbox is not None


def test_single_page_document_keyed_by_its_true_page_number_is_not_relabelled_page_1() -> None:
    """The sole page is cited by ITS number, not hardcoded to 1.

    ``sole_page`` reads the map's actual key, so a single-page map keyed {7: ...}
    cites 7. Guards against reintroducing a literal ``1`` as the "obvious" default.
    """
    recon = _reconcile_one("10 mg", page_no=None, pages={7: _PAGE_1})

    assert recon.supported is True
    assert recon.page_no == 7


# --- a page nobody rasterized ------------------------------------------------


def test_fact_naming_a_page_that_does_not_exist_is_unsupported() -> None:
    """A page_no absent from the map has no tokens to search — not page 1's.

    ``vision._check_page_numbers`` normally rejects a fabricated page number
    upstream, but reconciliation must not depend on a caller's guard for its own
    safety.
    """
    recon = _reconcile_one("10 mg", page_no=9)

    assert recon.supported is False
    assert recon.bbox is None


# --- the fixture's own premise ------------------------------------------------


def test_fixture_actually_distinguishes_pages() -> None:
    """Guards the guard: these tests are worthless if the pages are interchangeable.

    A single-page StubOcr fixture returns page 1's tokens for every page, which is
    why the whole suite missed this bug. If someone later flattens this fixture,
    the headline test would pass for the wrong reason — so assert the premise.
    """
    assert _PAGES[1] != _PAGES[2], "pages must carry DIFFERENT tokens"
    assert _PAGES[3] == [], "page 3 must have NO tokens — that is the trigger"
    page_1_text = " ".join(str(t["text"]) for t in _PAGE_1).lower()
    assert "10 mg" in page_1_text, "page 1 must coincidentally contain the value"
