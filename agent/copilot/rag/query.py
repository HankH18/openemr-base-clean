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
from collections.abc import Collection

from copilot.rag._lexical import tokenize

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

#: The clinical lexicon flattened into a token whitelist: every abbreviation key
#: AND every token of every expansion. A distilled query keeps these on the
#: strength of the closed lexicon alone (``dka``/``diabetic``/``ketoacidosis``),
#: so :func:`distill_clinical_terms` still recognises them even against an empty
#: or tiny corpus vocabulary. Built with the SAME
#: :func:`~copilot.rag._lexical.tokenize` the embedder / sparse leg / reranker
#: use, so "recognised" is measured in exactly the token space those legs consume.
_LEXICON_TERMS: frozenset[str] = frozenset(
    token
    for key, expansion in CLINICAL_ABBREVIATIONS.items()
    for token in (*tokenize(key), *tokenize(expansion))
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


def distill_clinical_terms(query: str, *, vocabulary: Collection[str] = ()) -> str:
    """Reduce a de-identified, expanded query to ONLY recognised clinical terms.

    This is a second, complementary PHI guard to
    :func:`~copilot.rag.deidentify.deidentify`, not a replacement for it. The
    scrubber removes identifiers by SHAPE, but a bare, unlabelled patient name has
    no shape to match — ``"Should John Doe get a statin?"`` leaves ``John Doe``
    intact (deidentify's own docstring states this residual), and that name would
    then egress verbatim to a third-party embedder (Voyage) or reranker (Cohere).
    This closes the leak from the other side: instead of trying to recognise every
    PHI shape, it keeps only tokens it can affirmatively recognise as CLINICAL and
    drops everything else, so an unrecognised token (a name) never egresses,
    whether or not deidentify caught it. Keep deidentify in front of this as
    defense-in-depth — distillation narrows what a scrub missed; it does not
    license removing the scrub.

    A token is kept iff it is a recognised clinical term — a member of the closed
    clinical lexicon (:data:`_LEXICON_TERMS`: :data:`CLINICAL_ABBREVIATIONS` keys
    and the tokens of their expansions) OR of ``vocabulary``, the caller's
    known-clinical-term set. The retriever passes the guideline corpus's own
    terms, which are public clinical text and never PHI. Tokens are de-duplicated
    and returned in first-appearance order as one space-joined string; the
    embedder and reranker consume a bag of terms, so order past determinism does
    not matter. Tokenisation uses the SAME
    :func:`~copilot.rag._lexical.tokenize` those legs use, so a token kept here is
    a token that would have overlapped the corpus in exactly the same form.

    **Recall tradeoff, stated honestly.** If nothing is recognised the result is
    the empty string. This deliberately does NOT fall back to egressing the raw
    query: that fallback is precisely the leak — a query that is *only* a name
    would then send the name. The remote leg embeds/reranks a smaller (possibly
    empty) bag, accepting reduced recall for that one query in exchange for never
    leaking an unrecognised token. On the keyless path this costs nothing: the
    stub embedder is lexical over the same tokenizer, and the corpus vocabulary
    already contains every clinical term the query shares with the corpus, so a
    dropped token is one no chunk carries — it could not have moved the ranking
    whether sent or not.

    Empty/whitespace input returns ``""``.
    """
    recognized = _LEXICON_TERMS.union(vocabulary)
    kept: list[str] = []
    seen: set[str] = set()
    for token in tokenize(query):
        if token in seen or token not in recognized:
            continue
        seen.add(token)
        kept.append(token)
    return " ".join(kept)
