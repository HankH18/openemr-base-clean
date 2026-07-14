"""Deterministic lexical scoring — the portable sparse/rerank backbone.

Shared by the SQLite sparse-retrieval fallback (``retriever``) and the keyless
stub reranker (``rerank``) so both rank text the same way: term overlap between
a query and a candidate, with a tiny clinical-safe stop-word list. Pure and
offline — no model, no network — so scores are byte-stable across runs.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

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
    """
    if not query_tokens:
        return 0.0
    doc_counts = Counter(tokenize(document))
    return float(sum(doc_counts[term] for term in set(query_tokens)))
