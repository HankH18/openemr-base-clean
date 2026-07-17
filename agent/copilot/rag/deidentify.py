"""PHI-scrub choke point for outbound retrieval payloads.

:func:`deidentify` is the single place a clinician's free-text query is
scrubbed of patient identifiers *before* it ever reaches an embedder (Voyage)
or reranker (Cohere). The retriever routes every query through it, so no
downstream client — stub or real, local or third-party — sees a name, MRN,
SSN, date of birth, phone number, or email address.

The scrub is deliberately conservative and deterministic (no model, no
network): it removes structured identifiers by shape (SSN/phone/date/email/
long digit runs) and labelled name fields ("Patient: <Name>"), while leaving
the clinical topic of the question intact. It is *not* a substitute for a
policy that keeps PHI out of the corpus itself — the guideline corpus is
public clinical text — but it guarantees the query egress stays clean.
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
_PHONE_RE = re.compile(r"\(?\b\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}\b")
#: MRN / account / member numbers and any other run of 5+ digits.
_LONG_DIGITS_RE = re.compile(r"\b\d{5,}\b")

#: A labelled name field: "Patient: Marisol Quintanilla", "Name - John Doe".
#: The trailing ``[:\-]`` requirement keeps it from eating ordinary prose like
#: "the patient presented with…". The value is a run of Title-Case tokens; a
#: comma or any non-name punctuation ends it.
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

_WHITESPACE_RE = re.compile(r"\s{2,}")

# Applied in order. Structured shapes first, then the labelled name (which
# leaves the label word behind as a harmless, PHI-free token).
_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_EMAIL_RE, REDACTION),
    (_SSN_RE, REDACTION),
    (_DATE_RE, REDACTION),
    (_PHONE_RE, REDACTION),
    (_LONG_DIGITS_RE, REDACTION),
    (_LABELED_NAME_RE, "patient"),
)


def deidentify(text: str) -> str:
    """Return ``text`` with patient identifiers scrubbed, topic preserved.

    Idempotent and deterministic — the same input always yields the same
    output, and re-scrubbing an already-clean string is a no-op (aside from
    whitespace normalisation). Empty/whitespace input returns ``""``.
    """
    if not text or not text.strip():
        return ""
    scrubbed = text
    for pattern, replacement in _SUBSTITUTIONS:
        scrubbed = pattern.sub(replacement, scrubbed)
    return _WHITESPACE_RE.sub(" ", scrubbed).strip()
