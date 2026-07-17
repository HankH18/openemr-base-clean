"""feat_rag criterion 3 — hybrid retrieve fuses sparse + dense with RRF.

FROZEN GOALS. The RRF fusion ordering is asserted against HAND-COMPUTED
fixtures. Both fixtures are rank-constant in the RRF constant k (proof in the
comments), so any standard reciprocal-rank-fusion implementation —
``score(d) = sum_i 1/(k + rank_i(d))`` with any fixed k > 0 and either 0- or
1-based ranks — must produce exactly these orderings.

Hand computation for fixture A (k = 60, 1-based ranks):
    sparse = [A, B, C, D]        dense = [C, A, E]
    A: 1/61 + 1/62 = 0.0325225   (sparse rank 1, dense rank 2)
    C: 1/63 + 1/61 = 0.0322664   (sparse rank 3, dense rank 1)
    B: 1/62       = 0.0161290
    E: 1/63       = 0.0158730
    D: 1/64       = 0.0156250
    => [A, C, B, E, D]
  k-invariance: A-C = 1/(k+2) - 1/(k+3) > 0; C has two terms incl. 1/(k+1) so
  C > B = 1/(k+2); and 1/(k+2) > 1/(k+3) > 1/(k+4) gives B > E > D. QED.

Hand computation for fixture B (k = 60, 1-based ranks):
    sparse = [P, Q, R]           dense = [R]
    R: 1/63 + 1/61 = 0.0322664
    P: 1/61       = 0.0163934
    Q: 1/62       = 0.0161290
    => [R, P, Q]   (k-invariant: R > P since R = P's term + 1/(k+3); P > Q.)
"""

from __future__ import annotations

from collections.abc import Mapping

import _rag_helpers as H


def _resolve_fuse():
    return H.feature_attr(
        ("copilot.rag.retriever", "copilot.rag.fusion", "copilot.rag.rrf", "copilot.rag"),
        ("rrf_fuse", "reciprocal_rank_fusion", "rrf"),
        "the RRF fusion callable, e.g. rrf_fuse(sparse_ids, dense_ids)",
    )


def _call_fuse(fuse, sparse: list[str], dense: list[str]):
    try:
        return fuse(sparse, dense)
    except TypeError:
        return fuse([sparse, dense])


def _normalize_ids(result) -> list[str]:
    if isinstance(result, Mapping):  # {id: score} — order by score desc (fixtures tie-free)
        return [k for k, _ in sorted(result.items(), key=lambda kv: -float(kv[1]))]
    out: list[str] = []
    for item in list(result):
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (tuple, list)) and item:
            out.append(str(item[0]))
        else:
            for attr in ("chunk_id", "id"):
                v = getattr(item, attr, None)
                if v is not None:
                    out.append(str(v))
                    break
            else:
                H.fail(f"cannot normalize fused-ranking item {item!r}")
    return out


def test_rag_03_rrf_fusion_matches_hand_computed_fixture():
    fuse = _resolve_fuse()

    # Fixture A — overlap in both rankings plus items unique to each side.
    sparse = ["chunk-A", "chunk-B", "chunk-C", "chunk-D"]
    dense = ["chunk-C", "chunk-A", "chunk-E"]
    got = _normalize_ids(_call_fuse(fuse, sparse, dense))
    expected = ["chunk-A", "chunk-C", "chunk-B", "chunk-E", "chunk-D"]
    assert got[: len(expected)] == expected, (
        f"RRF ordering diverged from the hand-computed fixture: got {got}, want {expected}"
    )
    assert len(got) == len(set(got)), f"fused ranking must not contain duplicates: {got}"
    assert set(got) == set(expected), (
        f"fused ranking must be the union of both input rankings: got {set(got)}"
    )

    # Fixture B — an item found by both retrievers beats every single-source item.
    got_b = _normalize_ids(_call_fuse(fuse, ["chunk-P", "chunk-Q", "chunk-R"], ["chunk-R"]))
    expected_b = ["chunk-R", "chunk-P", "chunk-Q"]
    assert got_b[: len(expected_b)] == expected_b, (
        f"RRF ordering diverged from the hand-computed fixture: got {got_b}, want {expected_b}"
    )
