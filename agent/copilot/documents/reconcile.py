"""Reconcile an extracted value back to the page's OCR tokens (no invention).

The vision model proposes a value; reconciliation is the deterministic gate that
decides whether that value is actually *on the page*. A value that matches a span
of OCR tokens gets that span's bounding box and a positive match confidence
(``supported=True``); a value found nowhere on the page is flagged
``supported=False`` with no bbox — surfaced as unverified rather than silently
trusted. This is the pixel-level evidence a later grounding pass re-checks.

"Matches" is deliberately two-sided, because ``supported=True`` is read as *this
value is on the page, here is where*. A span must both resemble the value
(:data:`_MATCH_MIN`, symmetric) and cover it in both directions
(:data:`_COVERAGE_MIN`, two-sided): the span must account for essentially all of
the value, and the value for essentially all of the span. Similarity alone would
bless a span that is merely a long enough *prefix* of the value — handing back a
box that omits the value's tail — or a span *longer* than the value, when the
value's characters are a subsequence of a different, longer printed token (a value
shrunk to ``18`` riding in on the page's ``180``). Both are the invention the gate
exists to catch, wearing the costume of evidence.

Matching is span-based because OCR emits one token per *word*: a value like
"Metformin 500 mg PO BID" is never a single token, only a run of adjacent ones.
Scoring against single tokens would report every honest multi-word extraction —
drug + dose + frequency, patient names — as unverified.

Adjacency in the token stream is *reading order*, which on a table is row-wise:
after the last word of a cell the stream jumps to the next column, not to the
cell's own wrapped tail. So a value printed as

    QHS (once daily,
    bedtime)

has its tail separated from its head by every other cell in the row. OCR line
metadata does not help — the engine calls the whole table row one "line" — but
geometry does: a wrapped tail drops one line-height and returns to the left edge
of the cell it continues. :func:`reconcile_value` therefore allows a span to
continue at that geometric wrap, in addition to (never instead of) the
contiguous stream.
"""

from __future__ import annotations

import unicodedata
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

# Minimum text similarity for a token span to count as the value's source. Exact
# matches score 1.0; this rejects incidental partial overlaps (e.g. a stray "."),
# so "999.9" — absent from the page — matches nothing.
_MATCH_MIN = 0.8

# Minimum fraction of characters the value and its winning span must share, required
# in *both* directions (see _coverage_ok).
#
# Similarity alone cannot carry the gate's claim. ratio() is 2*matched/(len_a+len_b)
# — symmetric — so it asks "do these two strings resemble each other", never "is all
# of the value here". A span that is a two-thirds-length *prefix* of the value scores
# 0.8 and passes, and the gate then reports supported=True with a bbox that does not
# cover the missing tail: the page prints "Metformin 500 mg PO", the model says
# "Metformin 500 mg PO BID", and a clinician clicking the highlight to check the
# schedule sees a box that never says BID. Coverage supplies the two halves similarity
# omits — matched characters over the *value's* length (a prefix omits the value's
# tail) and over the *span's* length (a span longer than the value is a different,
# longer token the value merely hides inside) — so support means the value was
# located, not merely resembled. Both must hold: similarity rejects a noisy match,
# coverage rejects a partial or a shrunk one.
#
# 0.95 is measured, not chosen: real Tesseract output for the three demo documents
# (200dpi, ~1180 word boxes) reconciled against their pages' own printed field values
# separates almost perfectly, because a value OCR reads at all it usually reads
# exactly:
#   * 93% of real values OCR located scored coverage 1.0; every remaining one scored
#     <= 0.93 — and those were not honest matches but spans of a *different* string
#     (value "(512) 555-0177" won a box over the printed "(512) 555-0110").
#   * The same values with one absent word appended — the failure being fixed —
#     scored 0.81 to 0.929 ("Once daily BID" -> "once daily (blood", 0.929 the worst).
# 0.95 clears that 0.929 ceiling with margin while admitting every value OCR read
# correctly. What it admits: ~1 mangled character per 20 of the value (2 in a 40-char
# medication line), which is the punctuation/glyph damage real OCR inflicts. What it
# rejects: any absent word — the shortest clinical ones ("PO", "BID", "PRN") cost 3-4
# characters, more than the budget for any value under ~60 characters — and, by the
# same measure, a short value whose few characters OCR did not reproduce (a member ID
# read one digit off is not a located member ID). 1.0 would be stricter than OCR is
# accurate; anything at or below 0.93 readmits the invented tails measured above.
#
# The band between 0.93 and 0.95 is where the measurement is loudest: of the honest
# values that land there, all but a handful are ones OCR *lost a whole word* of ("PO
# BID (twice daily) Type" came back without the "PO"; "H 0.70-1.30 mg/dL" without the
# abnormal-flag "H"), and those must be rejected — they are the same event as an
# invented word, seen from the other side. Loosening to 0.90 to keep them would
# readmit a fabricated route.
#
# The subsequence residue that value-side coverage alone left open is now closed by
# the span side. An invented word whose letters happen to sit in order in the
# adjacent text — "…06:05 CDT PRN" where "prn" is a subsequence of the "Printed" that
# follows, "…PO BID" where "bi" hides inside a following "Hemoglobin" — once covered
# the *value* 1.0 and was blessed. But reaching those letters means extending the
# span to include that neighbouring token, which makes the span longer than the
# value; the span side (matched over the *span's* length) then falls below 0.95 on
# the characters the neighbour added, and the match is refused. Coverage still counts
# characters, so an equal-length swap OCR itself made (the demo page's printed "·"
# read back as "-") stays a located value — which is the point.
_COVERAGE_MIN = 0.95

# Widest run of adjacent tokens ever joined into one candidate. Extracted values
# are short (a drug + dose + frequency, a patient name), so this bounds the search
# on a dense page. A value with more words than this reconciles to nothing and is
# surfaced as unverified — the safe direction for a no-invention gate.
_MAX_WINDOW_TOKENS = 12

# A span whose text length falls outside these multiples of the value's length can
# never clear _MATCH_MIN, so it is skipped without running the matcher. ratio() is
# 2*matched/(len_a + len_b) and matched can never exceed the shorter string, so any
# span's score is bounded by 2*min(len_value, len_span)/(len_value + len_span) —
# difflib's own real_quick_ratio. Solving that bound for _MATCH_MIN gives the two
# multiples below. These skip only spans that provably fail, so the winner is
# identical to scoring every span; they are an exact shortcut, not a heuristic.
#
# Coverage tightens the lower bound and leaves the upper one alone. Matched characters
# are a common subsequence, so matched <= len_span; a span shorter than
# _COVERAGE_MIN * len_value therefore cannot reach _COVERAGE_MIN and provably fails —
# the same *kind* of exact shortcut, now the binding one, since _COVERAGE_MIN (0.95)
# exceeds similarity's own floor (0.8/1.2 = 0.667). max() keeps whichever bound is
# tighter true if either constant is ever retuned. The upper bound stays similarity's
# own. The span side of coverage does imply a tighter one — matched <= len_value, so a
# span past len_value / _COVERAGE_MIN can never clear matched >= _COVERAGE_MIN*len_span
# — but that is left to the in-loop gate rather than folded in here: the shortcut only
# has to skip spans that *provably* fail, and past 1.5x it is similarity that already
# rules them out. Spans between the two bounds are still scored, then refused by
# two-sided coverage, so the winner is identical to scoring every span either way.
_MAX_LEN_RATIO = 2.0 / _MATCH_MIN - 1.0
_MIN_LEN_RATIO = max(_MATCH_MIN / (2.0 - _MATCH_MIN), _COVERAGE_MIN)

# Every geometric tolerance below is a multiple of the page's *own* median token
# height — the one length scale OCR always reports, and a direct proxy for type
# size. Ratios to a measured scale survive any DPI, page size, or normalization;
# a pixel constant tuned on one render breaks on the next.
#
# The ratios are deliberately coarse, because the signal they separate is not
# close: on the reference med list a wrapped tail returns to within 0.06 of a
# text-height of its cell's left edge, while the neighbouring column sits ~15
# text-heights away — a ~250x gap. Anything in that range decides identically,
# so these are bounds, not tuning.
_LINE_BAND = 0.5  # same visual line: y-centers within half a text-height
_WRAP_MIN_GAP = 0.5  # a wrap drops at least half a text-height...
_WRAP_MAX_GAP = 2.0  # ...and at most two; beyond that it is a new block, not a wrap
_COLUMN_TOL = 0.5  # "returns to the same left edge": within half a text-height
# Words inside a cell are a space apart (~0.35 of a text-height); the next column
# is several text-heights away. 1.5 sits in the empty valley between the two.
_CELL_GAP = 1.5


@dataclass(frozen=True)
class Reconciliation:
    """Outcome of locating one value in a page's OCR tokens."""

    supported: bool
    bbox: list[float] | None
    match_confidence: float
    page_no: int | None = None


@dataclass(frozen=True)
class _Layout:
    """Visual lines and length scales derived from a page's own token boxes."""

    line_of: list[int]  # token index -> visual line index
    line_tokens: list[list[int]]  # line index -> token indices, sorted by x
    line_xs: list[list[float]]  # line index -> those tokens' left edges, ascending
    line_y: list[float]  # line index -> mean y-center
    x_left: list[float]  # token index -> left edge
    run_end: list[int]  # token index -> last token of its cell run (see _page_layout)
    column_tol: float
    min_gap: float
    max_gap: float


@dataclass(frozen=True)
class _Scan:
    """The invariants of one ``reconcile_value`` call, hoisted out of the loops."""

    target: str
    texts: list[str]
    confs: list[float]
    max_window: int
    min_span_len: float
    max_span_len: float
    # Minimum per-token OCR confidence a span's weakest token may have and still
    # be legible enough to support (the decoupled legibility floor — see
    # reconcile_value). 0.0 admits any real match.
    conf_floor: float


def _token_field(token: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in token:
            return token[name]
    raise KeyError(f"OCR token is missing all of {names!r}")


def _normalize(text: str) -> str:
    # NFC first, so a composed and a decomposed spelling of the same accented
    # value ("José" as U+00E9 vs "e"+U+0301) fold to one string before matching.
    # Without it the two forms are unequal character sequences and the two-sided
    # coverage gate refuses the pair — the value fails closed and loses its
    # citation, exactly the invention the gate must not manufacture from noise.
    return unicodedata.normalize("NFC", text).strip().lower()


def _coverage_ok(target: str, text: str) -> bool:
    """Whether ``text`` accounts for essentially all of ``target`` *and vice versa*.

    Two-sided, from one matched-character count. ``get_matching_blocks`` returns
    disjoint blocks in order, so summing their sizes counts each matched character
    once — the length of the common subsequence difflib actually aligned, which is
    what "located" means here. Both fractions must clear :data:`_COVERAGE_MIN`:

    * ``matched / len(target)`` — the *value* side. Divides by the value's length,
      so a span that is a mere prefix, missing the value's tail, fails: the gate
      claims the value is on the page, not that the page resembles the value.
    * ``matched / len(text)`` — the *span* side. Divides by the span's length, so a
      span much longer than the value fails: the value's characters being a
      subsequence of a *different, longer* printed token is not the value being on
      the page. Without it a shrunk value rides in on the correct token — ``180``
      read as ``18`` (a subsequence of ``180``), ``88mcg`` as ``88mg``, ``-2.5`` as
      ``2.5`` — scoring value-side coverage 1.0 and getting certified ``supported``
      against the right box: a wrong value wearing the costume of evidence.

    Written ``matched >= _COVERAGE_MIN * len(...)`` rather than as a division, which
    is algebraically identical at the gate and sidesteps a zero-length edge.
    """
    matcher = SequenceMatcher(None, target, text)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched >= _COVERAGE_MIN * len(target) and matched >= _COVERAGE_MIN * len(text)


def _union_bbox(boxes: Sequence[Sequence[float]]) -> list[float]:
    """Smallest ``[x, y, w, h]`` covering every box in a winning span."""
    if len(boxes) == 1:
        # Returned verbatim: recomputing w as (x + w) - x would perturb a
        # single-token bbox in the last float digit.
        return [float(v) for v in boxes[0]]
    x0 = min(float(box[0]) for box in boxes)
    y0 = min(float(box[1]) for box in boxes)
    x1 = max(float(box[0]) + float(box[2]) for box in boxes)
    y1 = max(float(box[1]) + float(box[3]) for box in boxes)
    return [x0, y0, x1 - x0, y1 - y0]


def _page_layout(tokens: Sequence[Mapping[str, Any]]) -> _Layout | None:
    """Group tokens into visual lines and derive the page's own length scales.

    Returns ``None`` whenever the page cannot support a geometric wrap — too few
    tokens, or boxes that are missing/malformed/degenerate. Reconciliation then
    falls back to the contiguous path alone, which is the safe direction.

    Note the boxes are parsed defensively: ``reconcile_value`` otherwise reads a
    bbox only for the *winning* span, so a page whose tokens lack boxes must keep
    reconciling exactly as it does today rather than start raising here.

    The lines are derived from y-geometry alone. OCR block/paragraph/line
    metadata is deliberately unused: engines label an entire table *row* one
    "line", so it segments nothing. These lines likewise span the whole row —
    they answer only "what sits one line below", never "where does this cell end".
    """
    try:
        boxes = [[float(v) for v in _token_field(token, "bbox", "box")] for token in tokens]
    except (KeyError, TypeError, ValueError):
        return None
    if len(boxes) < 2 or any(len(box) != 4 for box in boxes):
        return None
    heights = sorted(box[3] for box in boxes)
    # The median token height is the page's type size, measured rather than
    # assumed — every tolerance below is a multiple of it.
    text_height = heights[len(heights) // 2]
    if text_height <= 0.0:
        return None

    x_left = [box[0] for box in boxes]
    y_center = [box[1] + box[3] / 2.0 for box in boxes]
    line_of = [0] * len(boxes)
    line_tokens: list[list[int]] = []
    anchor = 0.0
    for i in sorted(range(len(boxes)), key=lambda k: (y_center[k], x_left[k])):
        # Compared against the line's first y-center, not its previous one, so a
        # page of near-touching lines cannot drift into one giant cluster.
        if not line_tokens or y_center[i] - anchor > _LINE_BAND * text_height:
            line_tokens.append([])
            anchor = y_center[i]
        line_tokens[-1].append(i)
        line_of[i] = len(line_tokens) - 1
    for line in line_tokens:
        line.sort(key=lambda k: x_left[k])

    # A cell run: the maximal stretch of stream-adjacent tokens sharing a line
    # with no column-sized gap between them — i.e. the words of one cell on one
    # line. Scanned backwards so each token inherits its run's end in one pass.
    run_end = list(range(len(boxes)))
    for i in range(len(boxes) - 2, -1, -1):
        gap = x_left[i + 1] - (boxes[i][0] + boxes[i][2])
        if line_of[i + 1] == line_of[i] and gap <= _CELL_GAP * text_height:
            run_end[i] = run_end[i + 1]

    return _Layout(
        line_of=line_of,
        line_tokens=line_tokens,
        line_xs=[[x_left[i] for i in line] for line in line_tokens],
        line_y=[sum(y_center[i] for i in line) / len(line) for line in line_tokens],
        x_left=x_left,
        run_end=run_end,
        column_tol=_COLUMN_TOL * text_height,
        min_gap=_WRAP_MIN_GAP * text_height,
        max_gap=_WRAP_MAX_GAP * text_height,
    )


def _wrap_continuation(layout: _Layout, token: int, x_band: float) -> int | None:
    """The token that continues ``token``'s line as a wrap back to ``x_band``.

    A wrapped tail is the one thing that both drops about a line-height *and*
    returns to the left edge of the cell it continues; the row's next column does
    neither. Returns ``None`` unless a token satisfies both, so the ordinary case
    — no wrap here — costs a bisect that misses.
    """
    line = layout.line_of[token]
    below = line + 1
    if below >= len(layout.line_y):
        return None
    gap = layout.line_y[below] - layout.line_y[line]
    if gap < layout.min_gap or gap > layout.max_gap:
        return None  # a new block further down the page, not this line's wrap
    xs = layout.line_xs[below]
    for k in range(bisect_left(xs, x_band - layout.column_tol), len(xs)):
        if xs[k] > x_band + layout.column_tol:
            return None
        # Forward in the stream only: keeps chains acyclic, and a real wrap is
        # always read after the line it continues.
        if layout.line_tokens[below][k] > token:
            return layout.line_tokens[below][k]
    return None


def _best_contiguous_from(
    scan: _Scan, chain: Sequence[int], text: str, conf: float, limit: int | None = None
) -> tuple[float, list[int] | None]:
    """Best-scoring chain among ``chain`` and its contiguous stream extensions.

    Shared verbatim by the contiguous pass and by the tail of a wrapped chain, so
    both obey one definition of the length bounds, the window cap, and the
    weakest-token confidence rule.

    ``limit`` is the last token the chain may reach. The contiguous pass passes
    ``None`` — it claims only reading-order adjacency, and may cross a column as
    it always has. A wrapped chain passes its cell run's end, because it claims
    something stronger — *these tokens are one cell's text* — and a claim about a
    cell has to stop at that cell's edge on the wrapped line too. Without it the
    tail walks straight out of the cell into the next column's own wrapped tail.
    """
    span: list[int] = list(chain)
    best_score = 0.0
    best_chain: list[int] | None = None
    while True:
        if len(text) > scan.max_span_len:
            break  # every wider span is longer still — see _MAX_LEN_RATIO
        if len(text) >= scan.min_span_len:  # else too short, but widening may fix it
            similarity = SequenceMatcher(None, scan.target, text).ratio()
            # Coverage is checked last: it costs a second pass of the matcher, and
            # only a span that already resembles the value and would win is worth
            # asking about. A span that fails any gate never touches best_score, so
            # a weaker span that does clear them can still win — the winner is the
            # best-scoring span among those clearing ALL THREE gates: it resembles
            # the value (similarity), OCR read it legibly enough to trust
            # (conf >= conf_floor — the decoupled legibility floor, see
            # reconcile_value), and it actually covers the value (two-sided
            # coverage). conf is the span's weakest per-token confidence, so the
            # floor rejects a span the moment any one of its glyphs is illegible.
            if (
                similarity >= _MATCH_MIN
                and conf >= scan.conf_floor
                and similarity * conf > best_score
                and _coverage_ok(scan.target, text)
            ):
                best_score = similarity * conf
                best_chain = list(span)
        following = span[-1] + 1
        if len(span) >= scan.max_window or following >= len(scan.texts):
            break
        if limit is not None and following > limit:
            break
        span.append(following)
        text = f"{text} {scan.texts[following]}"
        # A span is only as trustworthy as its least legible word, so the weakest
        # token governs — never an average that could hide one.
        conf = min(conf, scan.confs[following])
    return best_score, best_chain


def reconcile_value(
    value: str,
    tokens: Sequence[Mapping[str, Any]],
    page_no: int = 1,
    threshold: float = 0.0,
) -> Reconciliation:
    """Locate ``value`` among ``tokens``; return its bbox + confidence, or flag it.

    Scores ``value`` against every span of 1..N adjacent tokens (N = the value's
    word count, capped at :data:`_MAX_WINDOW_TOKENS`) and returns the union bbox of
    the best-scoring span. ``tokens`` must be in reading order — that order is what
    makes a span contiguous on the page — which both OCR engines emit.

    A span may also continue at a *geometric* wrap: reading order is row-wise, so
    a value that wraps inside a table cell has its tail separated from its head by
    the rest of the row. Such a chain must strictly out-score every contiguous one
    to win, so the contiguous path is unchanged by its existence. The winning
    chain's tokens are unioned whether or not they are contiguous, giving a
    wrapped match a two-line bbox — which is what it actually occupies.

    Support is decoupled into two independent questions, because they fail for
    different reasons and a value can pass one while failing the other:

    * *Located* — is this value on the page? Answered by two-sided coverage
      (:func:`_coverage_ok`, >= :data:`_COVERAGE_MIN`) and similarity
      (>= :data:`_MATCH_MIN`). Both are CONFIDENCE-INDEPENDENT: they compare the
      value's characters against the printed span's, so they reject an invented or
      a *shrunk* value (``180`` read as ``18``) no matter how legibly it printed.
    * *Legible* — did OCR read the located span clearly enough to trust the read?
      Answered by ``threshold``: the span's weakest per-token OCR confidence
      (``min_conf``) must be >= ``threshold``. This gates OCR legibility ALONE; it
      is no longer folded into a ``similarity * conf`` product. So a value that is
      fully located (coverage 1.0, similarity 1.0) but carries one low-confidence
      glyph is NOT stripped of its citation merely because that glyph read faintly
      — the earlier product gate did exactly that, a false negative on a correctly
      extracted value (min_conf 0.55 against a 0.7 product bar rejected the value
      even at similarity 1.0).

    A value is ``supported`` only when it clears BOTH — located AND legible. The
    pipeline passes ``Settings.doc_extraction_confidence_threshold`` as the
    legibility floor; the default 0.0 means "any real token match is enough".
    ``match_confidence`` is still reported as ``similarity * min_conf`` (a useful
    quality score for ranking), but it no longer decides support.
    """
    target = _normalize(value)
    best_score = 0.0
    best_chain: list[int] | None = None
    if target:
        # Parsed once up front: the loops below revisit each token in up to
        # _MAX_WINDOW_TOKENS different spans.
        scan = _Scan(
            target=target,
            texts=[_normalize(str(_token_field(token, "text", "word"))) for token in tokens],
            confs=[float(_token_field(token, "conf", "confidence")) for token in tokens],
            max_window=min(len(target.split()), _MAX_WINDOW_TOKENS),
            min_span_len=len(target) * _MIN_LEN_RATIO,
            max_span_len=len(target) * _MAX_LEN_RATIO,
            conf_floor=threshold,
        )
        # Pass 1 — contiguous spans, the whole of what reconciliation used to be.
        for start in range(len(scan.texts)):
            score, chain = _best_contiguous_from(
                scan, [start], scan.texts[start], min(1.0, scan.confs[start])
            )
            if score > best_score:
                best_score = score
                best_chain = chain
        # Pass 2 — chains that take one geometric wrap. Kept a separate pass, and
        # scoring only chains that actually wrap, so it can never rewrite a
        # contiguous winner: displacing one takes a strictly better match. A
        # single-word value cannot span two tokens, so it never needs the layout.
        layout = _page_layout(tokens) if scan.max_window >= 2 else None
        if layout is not None:
            for start in range(len(scan.texts)):
                # The band is the window's own left edge: the tail of a wrapped
                # cell returns to where that cell began.
                x_band = layout.x_left[start]
                span = [start]
                text = scan.texts[start]
                conf = min(1.0, scan.confs[start])
                while len(text) <= scan.max_span_len:
                    wrapped = _wrap_continuation(layout, span[-1], x_band)
                    if wrapped is not None:
                        score, chain = _best_contiguous_from(
                            scan,
                            [*span, wrapped],
                            f"{text} {scan.texts[wrapped]}",
                            min(conf, scan.confs[wrapped]),
                            limit=layout.run_end[wrapped],
                        )
                        if score > best_score:
                            best_score = score
                            best_chain = chain
                    following = span[-1] + 1
                    # -1 leaves room in the window for the wrap token itself.
                    if len(span) >= scan.max_window - 1 or following >= len(scan.texts):
                        break
                    # The head of a wrapped chain is one cell's text too, so it
                    # stops at its own cell's edge rather than running into the
                    # next column and wrapping from there.
                    if following > layout.run_end[start]:
                        break
                    span.append(following)
                    text = f"{text} {scan.texts[following]}"
                    conf = min(conf, scan.confs[following])
    # No product-vs-threshold check here: the legibility floor is ``conf_floor``,
    # already applied per span during selection, so a chosen chain has ALREADY
    # cleared both the located gate (coverage + similarity) and the legible gate
    # (min_conf >= threshold). Re-comparing best_score (the similarity*conf product)
    # to threshold would reintroduce the coupling this decoupling removed.
    if best_chain is not None:
        boxes = [[float(v) for v in _token_field(tokens[i], "bbox", "box")] for i in best_chain]
        return Reconciliation(
            supported=True,
            bbox=_union_bbox(boxes),
            match_confidence=best_score,
            page_no=page_no,
        )
    return Reconciliation(supported=False, bbox=None, match_confidence=0.0, page_no=page_no)
