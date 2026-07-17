"""Deterministic lexical scoring — the sparse-retrieval / rerank backbone.

Two rankers live here, and the difference between them is the whole point:

- :func:`overlap_score` — a raw sum of query-term frequencies. No IDF, no
  length normalization, no saturation. It is what the keyless stub reranker
  (``rerank``) sorts by, and it is kept **exactly as it was** because that
  double's offline contract is pinned by the acceptance harness.
- :func:`bm25_scores` — the real sparse ranker (``retriever``). BM25 over the
  corpus, which is :func:`overlap_score` plus the three things that make a
  lexical ranker work: IDF, length normalization, and term-frequency
  saturation.

Why the second exists. ``overlap_score`` scores a term by how often it appears
and nothing else, so a term in 4 of 19 corpus chunks counts exactly as much as
a term in 1 of 19. Measured on the shipped ``corpus/``, that is not academic —
it served **INR-hold advice for a major haemorrhage**:

    "How do I reverse warfarin in major life-threatening bleeding?"
      supratherapeutic-inr-without-bleeding  overlap=6.0  {bleeding:3, warfarin:3}
      major-bleeding-on-warfarin             overlap=5.0  {major:1, life:1,
                                                           threatening:1,
                                                           bleeding:1, warfarin:1}

The wrong chunk wins 6.0 to 5.0 by repeating two *low*-information terms
(``bleeding``/``warfarin``: df=4/19 each — they are in every section of that
document) while the right chunk carries the three terms that actually name the
emergency (``major``/``life``/``threatening``: df=1/19 each, the maximum
possible evidence in this corpus). IDF is precisely the correction: weight a
term by how much it narrows the corpus down. Under BM25 the same comparison
inverts, because ``major``/``life``/``threatening`` are worth ~1.7x each what
``bleeding`` is worth.

Pure and offline — no model, no network. Deterministic to the bit: query terms
are summed in sorted order so a float sum can never depend on set-iteration
order (and therefore never on ``PYTHONHASHSEED``), mirroring the care
:func:`copilot.rag.embeddings._hash_feature` takes for the same reason.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Minimal stop-word set. Deliberately tiny: only structural English words that
#: carry no clinical signal, so terms like "insulin" or "begin" always count.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
        "for", "from", "how", "in", "into", "is", "it", "its", "of", "on", "or",
        "our", "that", "the", "their", "then", "there", "these", "this", "to",
        "was", "were", "what", "when", "which", "who", "will", "with",
    }
)


def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens with structural stop-words removed."""
    return [tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS]


def overlap_score(query_tokens: Sequence[str], document: str) -> float:
    """Sum of query-term frequencies found in ``document``.

    A document that repeats a query term scores higher than one that mentions
    it once; a document sharing no query terms scores ``0.0``. Distinct query
    terms are considered once each (a duplicated query word is not double
    counted).

    .. warning::
       **Not a retrieval ranker.** No IDF, so every term counts the same
       regardless of how much it narrows the corpus (see the module docstring
       for the major-haemorrhage case this loses). Retained only for the
       keyless :class:`~copilot.rag.rerank.StubReranker`, whose deterministic
       offline behaviour the acceptance harness pins. Sparse retrieval uses
       :func:`bm25_scores`.
    """
    if not query_tokens:
        return 0.0
    doc_counts = Counter(tokenize(document))
    return float(sum(doc_counts[term] for term in set(query_tokens)))


#: BM25 term-frequency saturation. The literature default (Robertson & Zaragoza,
#: "The Probabilistic Relevance Framework: BM25 and Beyond"; Lucene ships the
#: same value). **Deliberately not tuned to this corpus**: a constant fitted to
#: the handful of queries anyone happens to probe would encode those queries
#: rather than a ranking principle, and would look fixed while generalising
#: nowhere. The measured improvement is a property of using IDF at all, not of
#: this number — it holds unchanged across the standard k1 range.
BM25_K1 = 1.2

#: BM25 length-normalization strength (0 = none, 1 = full). The literature
#: default, and not tuned here either — see :data:`BM25_K1`.
BM25_B = 0.75


def bm25_scores(query_tokens: Sequence[str], documents: Mapping[str, str]) -> dict[str, float]:
    """Score every document against the query with BM25 (higher is better).

    The standard sparse ranker, and the three things it adds to a bare
    term-frequency sum — each of which the shipped corpus needed:

    1. **IDF** — a term is weighted by how much it narrows the corpus down:
       ``ln(1 + (N - df + 0.5) / (df + 0.5))``. This is the Lucene-variant
       smoothing, chosen over the textbook ``ln((N - df + 0.5) / (df + 0.5))``
       because the latter goes **negative** for a term in more than half the
       corpus — letting a common term actively subtract from a document that
       contains it, which is indefensible for a 19-chunk clinical corpus where
       "acute" is in 6 of 19. The ``1 +`` keeps every weight positive.
    2. **Saturation** — the 5th mention of a term adds far less than the 2nd
       (:data:`BM25_K1`), so a chunk cannot win by repetition alone. This is
       what the raw sum got wrong on the warfarin query.
    3. **Length normalization** — a long chunk does not out-score a short
       on-topic one merely by containing more words (:data:`BM25_B`).

    Corpus statistics come from ``documents`` itself: BM25 is defined over a
    collection, so the caller passes the whole candidate set (``id -> text``)
    rather than one document at a time. That is why this cannot live behind the
    per-text :class:`~copilot.rag.embeddings.Embedder` protocol, and why the
    keyless stub embedder can never supply IDF however good its hashing is.

    Deterministic: terms are summed in sorted order, so the float sum does not
    depend on set-iteration order. An empty query, or an empty collection,
    scores nothing rather than raising.
    """
    if not query_tokens or not documents:
        return {}

    tokenized = {doc_id: tokenize(text) for doc_id, text in documents.items()}
    n_docs = len(tokenized)
    doc_freq: Counter[str] = Counter()
    for tokens in tokenized.values():
        doc_freq.update(set(tokens))
    total_len = sum(len(tokens) for tokens in tokenized.values())
    avg_len = total_len / n_docs

    scores: dict[str, float] = {}
    for doc_id, tokens in tokenized.items():
        counts = Counter(tokens)
        # A zero-length corpus would make the length ratio undefined; treat a
        # degenerate (all-empty) collection as unit-length rather than dividing
        # by zero — every document is then equally (un)normalized.
        length_ratio = (len(tokens) / avg_len) if avg_len > 0.0 else 1.0
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * length_ratio)
        score = 0.0
        for term in sorted(set(query_tokens)):
            freq = counts.get(term, 0)
            if not freq:
                continue
            idf = math.log(1.0 + (n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            score += idf * (freq * (BM25_K1 + 1.0)) / (freq + norm)
        scores[doc_id] = score
    return scores
