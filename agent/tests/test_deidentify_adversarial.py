"""Adversarial de-identification: the scrub must keep the guarantee it now states.

An outside auditor ran the real ``deidentify`` and found the docstring promised
more than the code delivered — labelled names without a colon, alphanumeric MRNs,
and textual dates all passed through untouched. This suite pins BOTH halves of the
now-honest contract:

- **MUST SCRUB** — the previously-observed misses that are now caught (label-word
  names without a separator, alphanumeric MRNs, textual DOBs), PLUS the existing
  structured identifiers (SSN / email / phone / 5+-digit runs) as a regression
  guard so a future edit cannot silently reopen a hole.
- **MUST NOT SCRUB** — real clinical text. Over-scrubbing is a real harm: eating a
  drug strength, a lab analyte, an eponymous score, or an ICD code would silently
  degrade retrieval and could change which guideline a clinician receives. This
  corpus is asserted UNCHANGED (its clinical content survives verbatim). It is as
  load-bearing as the scrub corpus.
- **HONEST RESIDUAL** — one thing the module docstring explicitly says it does NOT
  catch (a bare, unlabelled name) is asserted to pass through, so the documented
  contract and the code cannot drift apart.

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import pytest

from copilot.rag.deidentify import deidentify


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch.isdigit())


# --- MUST SCRUB: the five observed misses (now caught) ------------------------


@pytest.mark.parametrize(
    ("query", "leaked_tokens", "clinical_survivors"),
    [
        # Label word, name follows, NO colon — the two observed name misses.
        (
            "pt Robert Smith presented with chest pain",
            ("robert", "smith"),
            ("chest pain",),
        ),
        (
            "patient May Day needs a flu shot",
            ("may day",),
            ("flu shot",),
        ),
        # A bare "patient name" label with no colon still scrubs.
        (
            "patient name Jordan Rivera has sepsis",
            ("jordan", "rivera"),
            ("sepsis",),
        ),
        # Alphanumeric MRN — the digit run is glued to a letter, so the pure-digit
        # rule's word boundary never fired. Now caught by the alnum-identifier rule.
        (
            "MRN A1234567 has diabetes",
            ("a1234567",),
            ("diabetes",),
        ),
        (
            "member ABC1234567 with CKD stage 3",
            ("abc1234567",),
            ("ckd",),
        ),
        # Textual DOB — month name, day, 4-digit year, in either order.
        (
            "DOB March 14, 1962 with hypertension",
            ("march 14", "1962"),
            ("hypertension",),
        ),
        (
            "born 14 Mar 1962, now with heart failure",
            ("14 mar", "1962"),
            ("heart failure",),
        ),
    ],
)
def test_must_scrub_now_catches_the_observed_misses(
    query: str, leaked_tokens: tuple[str, ...], clinical_survivors: tuple[str, ...]
) -> None:
    out = deidentify(query)
    low = out.lower()
    for token in leaked_tokens:
        assert token not in low, f"identifier {token!r} leaked through: {out!r}"
        # A numeric identifier must not survive as a bare digit run either.
        digits = _digits(token)
        if digits:
            assert digits not in _digits(out), f"identifier digits {digits!r} leaked: {out!r}"
    for survivor in clinical_survivors:
        assert survivor in low, f"clinical content {survivor!r} was destroyed: {out!r}"


@pytest.mark.parametrize(
    ("query", "leaked"),
    [
        # Structured-identifier regression guard: these already worked and MUST
        # keep working — a future edit cannot quietly reopen the known-good holes.
        ("SSN 123-45-6789 on file", "123456789"),
        ("reach me at (555) 014-2977 anytime", "5550142977"),
        ("alt phone 555-014-2977", "5550142977"),
        ("account number 998877665 active", "998877665"),
        ("visit date 03/14/1962 recorded", "03141962"),
        ("iso date 1962-03-14 recorded", "19620314"),
    ],
)
def test_must_scrub_structured_identifier_digits_never_leak(query: str, leaked: str) -> None:
    assert leaked not in _digits(deidentify(query)), f"{leaked!r} leaked from {query!r}"


def test_must_scrub_email_is_redacted() -> None:
    out = deidentify("contact jane.roe@example.com re: labs")
    assert "@" not in out and "jane.roe" not in out.lower()
    assert "labs" in out.lower()


# --- MUST NOT SCRUB: real clinical text survives (the over-scrub guard) --------
#
# Each string is asserted UNCHANGED. If a pattern (especially the alnum-MRN rule)
# scrubs any of these it is too aggressive — the fix is the pattern, not the test.
_CLINICAL_CORPUS: tuple[str, ...] = (
    "troponin 0.5 ng/mL",
    "CHA2DS2-VASc score 4",
    "BP 120/80",
    "HbA1c 7.2%",
    "metformin 500 mg BID",
    "E11.9",  # ICD-10 code (type 2 diabetes)
    "I10",  # ICD-10 code (essential hypertension)
    "T2DM",
    "Glasgow Coma Scale 15",
    "vitamin B12 deficiency",
    "COVID19 pneumonia",
    "H1N1 influenza",
    "SpO2 92% on room air",
    "lisinopril 10 mg daily",
    "atorvastatin 40 mg at bedtime",
    "warfarin dosing per INR",
    # Single Title-Case clinical phrase after a bare label — preserved because the
    # no-separator name rule requires two Title-Case tokens.
    "Patient Safety bundle",
    "Patient Education materials",
)


@pytest.mark.parametrize("clinical", _CLINICAL_CORPUS)
def test_must_not_scrub_clinical_text_survives_verbatim(clinical: str) -> None:
    assert deidentify(clinical) == clinical


def test_must_not_scrub_clinical_content_survives_in_a_sentence() -> None:
    # A realistic query mixing several must-not tokens: every clinical signal
    # must reach retrieval intact.
    query = "In T2DM with HbA1c 7.2%, is metformin 500 mg BID plus CHA2DS2-VASc 4 an indication?"
    out = deidentify(query)
    for token in ("T2DM", "HbA1c", "7.2%", "metformin", "500 mg", "CHA2DS2-VASc"):
        assert token in out, f"clinical signal {token!r} must survive: {out!r}"


# --- HONEST RESIDUAL: the docstring and the code agree ------------------------


def test_residual_bare_unlabelled_name_passes_through() -> None:
    # The module docstring is explicit: an unlabelled bare name is NOT guaranteed
    # scrubbed (regex cannot tell it from any capitalised word without NER, which
    # is out of scope). Assert that stated residual is real, so the doc cannot
    # drift into a false promise.
    query = "Should John Doe get a statin?"
    out = deidentify(query)
    assert "John Doe" in out, (
        "if this starts scrubbing bare unlabelled names, update the module "
        f"docstring to match — the contract must stay honest: {out!r}"
    )


def test_residual_month_year_without_a_day_is_preserved() -> None:
    # The docstring states a month-year with no day is left intact (a probable
    # clinical timeline, not a birth date). Pin that so the textual-date rule is
    # not quietly broadened into eating treatment history.
    assert "March 2021" in deidentify("started insulin in March 2021")
