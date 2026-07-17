"""Deterministic, keyless query expansion — a contextual-retrieval upgrade.

Clinicians type telegraphic, abbreviation-heavy questions ("DKA insulin?",
"AKI nephrotoxins to hold"), while the guideline corpus spells the conditions
out in full ("diabetic ketoacidosis", "acute kidney injury"). Sparse
term-overlap retrieval scores nothing on an abbreviation the corpus never
uses, and the dense stub embeds the surface string, so the recall gap is real.

:func:`expand_query` closes it by rewriting common inpatient/hospitalist
abbreviations to their full terms and *appending* the expansions to the query,
so both the abbreviation and its expansion are available to the sparse
(term-overlap / Postgres full-text) and dense ranking stages. It is:

- **Deterministic** — a fixed lexicon, matched by regex; no model, no network,
  byte-stable across runs (so the acceptance harness stays reproducible).
- **Append-only** — the original query is preserved verbatim; expansion only
  ever *adds* recall terms, it never drops or reorders the clinician's words.
- **Keyless** — nothing here has an external dependency.

**Invariant — run only on de-identified text.** The retriever routes the query
through :func:`copilot.rag.deidentify.deidentify` *first* and hands the scrubbed
string to :func:`expand_query`. Expansion works purely from a closed clinical
lexicon (no digits, no names), so it can neither re-introduce nor expand a
patient identifier; keeping it strictly after the choke point guarantees it
never sees raw PHI in the first place.
"""

from __future__ import annotations

import re

#: Inpatient/hospitalist abbreviation lexicon (lower-case key -> full term).
#: Deliberately limited to abbreviations that are *not* also common English
#: words, so case-insensitive matching can never mangle ordinary prose. Every
#: entry aligns with the demo corpus's topics (AKI, DKA, sepsis, warfarin) or
#: the abbreviation families a clinician reaches for most (Sx/Dx/Rx …).
CLINICAL_ABBREVIATIONS: dict[str, str] = {
    # Renal
    "aki": "acute kidney injury",
    "ckd": "chronic kidney disease",
    "atn": "acute tubular necrosis",
    "rrt": "renal replacement therapy",
    # Endocrine / metabolic
    "dka": "diabetic ketoacidosis",
    "hhs": "hyperosmolar hyperglycemic state",
    "sglt2": "sodium-glucose cotransporter 2 inhibitor",
    # Cardiovascular
    "htn": "hypertension",
    "chf": "congestive heart failure",
    "afib": "atrial fibrillation",
    "arb": "angiotensin receptor blocker",
    "arbs": "angiotensin receptor blockers",
    # Pulmonary / infectious
    "copd": "chronic obstructive pulmonary disease",
    "uti": "urinary tract infection",
    "sirs": "systemic inflammatory response syndrome",
    "qsofa": "quick sequential organ failure assessment",
    "mrsa": "methicillin-resistant staphylococcus aureus",
    "abx": "antibiotics",
    # Thrombosis / anticoagulation
    "dvt": "deep vein thrombosis",
    "vte": "venous thromboembolism",
    "inr": "international normalized ratio",
    "pcc": "prothrombin complex concentrate",
    "ffp": "fresh frozen plasma",
    "lmwh": "low molecular weight heparin",
    # Labs / studies
    "bmp": "basic metabolic panel",
    "abg": "arterial blood gas",
    "vbg": "venous blood gas",
    # General clinical shorthand
    "sx": "symptoms",
    "dx": "diagnosis",
    "rx": "treatment",
    "tx": "treatment",
    "hx": "history",
    "nsaid": "nonsteroidal anti-inflammatory drug",
    "nsaids": "nonsteroidal anti-inflammatory drugs",
}

#: Word-boundaried, case-insensitive matcher over the lexicon keys. Longer keys
#: first so ``nsaids`` is preferred over ``nsaid`` when the engine picks an
#: alternative (word boundaries already prevent a partial match either way).
_ABBREV_RE: re.Pattern[str] = re.compile(
    r"\b(?:"
    + "|".join(re.escape(key) for key in sorted(CLINICAL_ABBREVIATIONS, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)


def expand_query(query: str) -> str:
    """Return ``query`` with recognised clinical abbreviations expanded inline.

    The original text is preserved verbatim; each recognised abbreviation's full
    term is appended once, in first-appearance order, unless that full term is
    already present in the query. Empty/whitespace input returns ``""``.

    Deterministic and keyless — safe to call on the de-identified query before
    any embedder/reranker egress (see the module docstring's PHI invariant).
    """
    text = query.strip()
    if not text:
        return ""

    lowered = text.lower()
    additions: list[str] = []
    seen: set[str] = set()
    for match in _ABBREV_RE.finditer(text):
        expansion = CLINICAL_ABBREVIATIONS[match.group(0).lower()]
        key = expansion.lower()
        if key in seen or key in lowered:
            continue
        seen.add(key)
        additions.append(expansion)

    if not additions:
        return text
    return f"{text} {' '.join(additions)}"
