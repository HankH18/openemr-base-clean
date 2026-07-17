"""Best-effort PHI-scrub choke point for outbound retrieval payloads.

:func:`deidentify` is the single place a clinician's free-text query is scrubbed
before it reaches an embedder (Voyage) or reranker (Cohere). The retriever routes
every query through it. It is deterministic and offline (no model, no network,
no gazetteer): it removes identifiers by *shape*, and label-anchored names by the
label that precedes them, while leaving the clinical topic intact.

**What this function GUARANTEES to redact** (structured identifiers, matched by
shape whether labelled or not):

- email addresses;
- US SSNs in the ``3-2-4`` dashed shape;
- phone numbers in the dashed / parenthesised 10-digit shapes;
- numeric dates (``03/14/1962``, ``3-14-62``, ISO ``1962-03-14``);
- textual dates that carry an explicit day **and** a 4-digit year
  (``March 14, 1962``, ``14 Mar 1962``);
- any run of 5+ digits (MRN / account / member numbers, ZIP-in-context, …);
- alphanumeric identifiers — a single token that mixes letters with a run of 5+
  digits (``A1234567``, ``ABC1234567``);
- colon/dash-**labelled** names (``Patient: John Doe``, ``Name - John Doe``);
- names after a bare ``patient`` / ``pt`` label with **no** separator, when two
  or more Title-Case tokens follow the label (``pt Robert Smith``,
  ``patient May Day``).

**What this function does NOT guarantee — an operator MUST assume these can leak**
(they are the deliberate residual of a regex-only, over-scrub-averse scrub):

- **Bare, unlabelled names** with no preceding label and no separator —
  ``Should John Doe get a statin?`` passes through UNCHANGED. Regex cannot tell a
  name from any other capitalised word without an NER model or a name gazetteer,
  and both are out of scope here (over-scrubbing a drug/analyte/eponym would
  silently degrade retrieval, a real harm).
- A **single** capitalised token after a bare label (``pt Robert``) — not
  consumed, because the two-token rule that catches ``pt Robert Smith`` is also
  what keeps ``Patient Safety`` / ``Patient Education`` intact.
- Alphanumeric identifiers whose digit run is **shorter than 5** (``AB12``) —
  below the identifier threshold and indistinguishable from clinical tokens.
- **Month-year** textual dates with no day (``March 1962``) — preserved as a
  likely clinical timeline rather than a birth date.

**DO NOT read this as "safe to enable a third-party embedder/reranker on
arbitrary free text."** It is BEST-EFFORT: it reduces, but does not eliminate,
PHI in the query. Enabling Voyage/Cohere sends arbitrary clinician free-text to a
third party, and an unlabelled bare name will reach them. Keep PHI out of the
query at the source; this choke point is a backstop, not a licence.

The scrub is also over-scrub-*averse* by design: names after a bare label are
consumed only when 2+ Title-Case tokens follow, so a single clinical phrase
(``Patient Safety``) survives — but a multi-word Title-Case clinical phrase
immediately after a bare label (``Patient Health Questionnaire``) may be
over-redacted. This function is *not* a substitute for keeping PHI out of the
corpus itself — the guideline corpus is public clinical text.
"""

from __future__ import annotations

import re

#: What every scrubbed span collapses to. No digits and no capitalised words,
#: so a redaction can never re-introduce a PHI shape or seed a name match.
REDACTION = "[redacted]"

# --- structured identifiers (matched by shape, labelled or not) ----------------
#
# Order matters: the most specific shapes run first so a broad pattern never
# fragments a more specific one (e.g. SSN before the generic date/phone forms).

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DATE_RE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"  # 03/14/1962, 3-14-62
    r"|\b\d{4}-\d{2}-\d{2}\b"  # 1962-03-14 (ISO)
)

#: Month names, full and abbreviated (plus the common "Sept"), for textual dates.
_MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
#: Textual dates that carry BOTH a day and a 4-digit year, in either order:
#: "March 14, 1962" / "March 14 1962" and "14 Mar 1962" / "14 March 1962".
#: A day is REQUIRED — a bare month+year ("March 1962") is left intact as a
#: probable clinical timeline rather than a birth date, so treatment-history
#: phrasing is not silently destroyed.
_TEXT_DATE_RE = re.compile(
    r"\b(?:" + _MONTHS + r")\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:" + _MONTHS + r")\.?,?\s+\d{4}\b",
    re.IGNORECASE,
)

_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
#: MRN / account / member numbers and any other run of 5+ digits.
_LONG_DIGITS_RE = re.compile(r"\b\d{5,}\b")
#: Alphanumeric identifier: a single token that mixes letters with a run of 5+
#: digits — ``A1234567``, ``ABC1234567``, ``1234567A``. This is the SAME 5+-digit
#: policy :data:`_LONG_DIGITS_RE` already enforces, extended to tokens where the
#: digits are glued to letters (``\b\d{5,}\b`` misses those: there is no word
#: boundary between the "A" and the "1", so the digit run is never anchored).
#:
#: False-positive analysis — it must NOT fire on clinical tokens. The two
#: lookaheads require (a) at least one letter and (b) a run of >=5 contiguous
#: digits *in the same token*. No clinical token in scope has a 5+-digit run:
#: ``CHA2DS2-VASc`` (single digits), ``HbA1c`` / ``T2DM`` / ``B12`` / ``H1N1`` /
#: ``COVID19`` (1-2 digit runs), ``E11.9`` (2), ``500mg`` (3). Because the digit
#: threshold equals the existing ``_LONG_DIGITS_RE`` threshold, this introduces no
#: over-scrub the pure-digit rule did not already impose — it only closes the
#: letter-glued gap. The one residual over-scrub is a *glued* 5+-digit measurement
#: (e.g. ``50000units``), which is unusual (the standard form spaces the unit, and
#: a spaced ``50000`` is already redacted by ``_LONG_DIGITS_RE`` anyway).
_ALNUM_ID_RE = re.compile(r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d{5})[A-Za-z0-9]+\b")

#: A labelled name field with a separator: "Patient: Marisol Quintanilla",
#: "Name - John Doe". The trailing ``[:\-]`` requirement keeps it from eating
#: ordinary prose like "the patient presented with…". The value is a run of
#: Title-Case tokens; a comma or any non-name punctuation ends it.
#: The ``(?i:...)`` is SCOPED to the label on purpose. A leading ``(?i)`` would
#: apply case-insensitivity to the value too, so ``[A-Z]`` would match a lowercase
#: letter and the greedy run would swallow ordinary clinical prose:
#: ``"pt: severe sepsis with lactate elevation"`` collapsed to ``"patient"``,
#: destroying the query before retrieval. The label matches in any case; the value
#: must be genuinely Title-Case to be treated as a name.
_LABELED_NAME_RE = re.compile(
    r"\b(?i:patient(?:\s+name)?|pt|name)\b\s*[:\-]\s*"
    r"[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)*"
)

#: A labelled name with NO separator: "pt Robert Smith", "patient May Day".
#: Deliberately narrower than the separator form:
#: - only ``patient`` / ``pt`` (not bare ``name``, which appears in prose such as
#:   "the drug name Aspirin");
#: - the value tokens are strict Title-Case ``[A-Z][a-z]+`` — an uppercase letter
#:   THEN a lowercase one — so all-caps clinical acronyms (``AKI``, ``DKA``,
#:   ``CHA2DS2``) can never be read as a name;
#: - at least TWO such tokens are required and at most three consumed. Requiring
#:   two keeps common single-word clinical phrases ("Patient Safety", "Patient
#:   Education") intact while still catching a first+last name; capping at three
#:   bounds how far into the (usually lowercase) clinical continuation it can run.
#: Residual, stated honestly: a multi-word Title-Case clinical phrase right after
#: a bare label ("Patient Health Questionnaire") may be over-consumed, and a
#: single-token name ("pt Robert") is not caught.
_LABELED_NAME_NOSEP_RE = re.compile(
    r"\b(?i:patient(?:\s+name)?|pt)\b\s+"
    r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}"
)

_WHITESPACE_RE = re.compile(r"\s{2,}")

# Applied in order. Structured shapes first, then the labelled names (which leave
# the label word behind as a harmless, PHI-free token). The separator form runs
# before the no-separator form so the specific colon/dash match is preferred.
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_EMAIL_RE, REDACTION),
    (_SSN_RE, REDACTION),
    (_DATE_RE, REDACTION),
    (_TEXT_DATE_RE, REDACTION),
    (_PHONE_RE, REDACTION),
    (_LONG_DIGITS_RE, REDACTION),
    (_ALNUM_ID_RE, REDACTION),
    (_LABELED_NAME_RE, "patient"),
    (_LABELED_NAME_NOSEP_RE, "patient"),
)


def deidentify(text: str) -> str:
    """Return ``text`` with patient identifiers scrubbed, topic preserved.

    Best-effort and deterministic — see the module docstring for the exact
    guarantee and its residuals. Idempotent: re-scrubbing an already-clean string
    is a no-op (aside from whitespace normalisation), because neither replacement
    token (``[redacted]`` / ``patient``) re-triggers any pattern. Empty/whitespace
    input returns ``""``.
    """
    if not text or not text.strip():
        return ""
    scrubbed = text
    for pattern, replacement in _SUBSTITUTIONS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return _WHITESPACE_RE.sub(" ", scrubbed).strip()
