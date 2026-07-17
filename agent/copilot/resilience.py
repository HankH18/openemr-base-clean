"""Timeout budgets + bounded, jittered retry for every outbound call.

One module so that every egress budget in the service is stated — and defended —
in a single place, rather than being an implicit SDK default nobody chose.

**Timeouts are SLO-derived, not folklore.** Each constant below cites the
committed SLO it is anchored to (``OBSERVABILITY.md`` §7.1 and its alert
definitions). The governing idea: a timeout is not a latency *target*, it is the
point at which a call has already blown its SLO so badly that nothing useful can
still come back — so stop waiting and let the caller's fail-safe run. That makes
the SLO, not taste, the thing that sets the number.

**Retries never change steady-state p95.** A healthy call never retries, so the
budgets below only extend latency when an upstream is *already* failing — which
is exactly when the SLO alert firing is the correct outcome, not a regression.

What is retried (and nothing else):

- **timeouts** and **connection errors** (:class:`httpx.TimeoutException`,
  :class:`httpx.NetworkError`) — the request may never have been served;
- **429** and **5xx** (plus **408**, which is literally "request timeout") — the
  upstream explicitly said "transient, try again".

A **4xx is never retried**: it is a deterministic verdict about the request, and
re-sending it unchanged can only produce the identical 4xx. This is also the
property that makes retrying a single-use credential grant safe (see
``copilot.fhir.auth``): the server enforces single-use and reports it as a 4xx,
which terminates the loop rather than hammering it.

**Not for writes.** Nothing here is applied to ``copilot.fhir.write_client``,
which stays fail-closed on transport error: a clinical write whose success cannot
be *confirmed* must be reported FAILED, never retried into a possible duplicate
row. Retrying a clinical write is worse than failing it. See that module's
docstring for the full argument.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

# --- timeout budgets --------------------------------------------------------

#: Every budget below splits out the TCP/TLS connect phase at 5s. A connect that
#: has not completed in 5s is a routing/DNS problem, not a slow peer, and no
#: amount of further waiting fixes it. (Also the Anthropic SDK's own default
#: connect budget — the read budget is the part it gets catastrophically wrong.)
_CONNECT = 5.0

#: **Chat turn** (``copilot.agent.claude``). Anchor: ``chat`` p95 > 8s warns and
#: **p99 > 15s pages** (OBSERVABILITY.md Alert 2), over a turn that "calls
#: Anthropic 2-3x". A *single* model call exceeding 20s has therefore already
#: blown the whole turn's page threshold on its own — its answer is worthless by
#: the time it lands, so cut it. 20s still leaves generous headroom over a
#: healthy 2048-``max_tokens`` generation (typically 3-8s), so a slow-but-live
#: call is never aborted. Replaces the inherited 600s read timeout, which let one
#: hung call hold a clinician for ten minutes.
CHAT_TIMEOUT = httpx.Timeout(20.0, connect=_CONNECT)

#: **Vision extraction** (``copilot.documents.vision``). Anchor: document
#: ingestion p95 < 12s warn / **< 30s page** (§7.1), a budget the vision call
#: dominates (COST_ANALYSIS.md §9c estimates ~5s p50 / ~10s p95 for it).
#: Set at 2x the page threshold, deliberately *not* at it: past 30s the SLO
#: already pages, but the run may still be alive, and killing it exactly at the
#: page line converts a slow success into a ``failed`` document with **zero**
#: extracted facts (ingestion is fail-closed). 60s says "by now it is dead, not
#: slow". This is the loosest budget in the module because it is the only one no
#: clinician is synchronously blocked on, and it also covers the write phase —
#: page images are a large multi-MB base64 upload.
VISION_TIMEOUT = httpx.Timeout(60.0, connect=_CONNECT)

#: **Gating / critic safety pass** (``copilot.graph.critic``). Anchor: the
#: ``chat`` p95 < 8s warn (Alert 2) — this call runs *inside* a clinician's turn.
#: The tightest budget in the module, because it is the cheapest call to abandon:
#: it is advisory and **fail-safe** (a timeout falls back to the deterministic
#: citation partition and the answer is still served), it runs on the cheap
#: gating model, and it returns a tiny list of indices (1024 ``max_tokens``,
#: typically < 2s). If it alone consumed the entire turn's p95 budget the turn is
#: breaching anyway — take the deterministic verdict and serve.
GATING_TIMEOUT = httpx.Timeout(8.0, connect=_CONNECT)

#: **Voyage embedding** (``copilot.rag.embeddings``). Anchor: evidence-retrieval
#: p95 < 800ms warn / < 2s page (§7.1), of which the query embed is one leg
#: (p95 ~200ms for a single short text). 15s is far above that because the same
#: embedder also serves **corpus ingest**, which embeds large batches offline and
#: is not on any interactive SLO — one client, two workloads, so the budget is
#: sized for the slower one. Still halves the previous 30s.
EMBEDDING_TIMEOUT = httpx.Timeout(15.0, connect=_CONNECT)

#: **Cohere rerank** (``copilot.rag.rerank``). Anchor: the same 800ms warn / 2s
#: page retrieval budget. Rerank is a *quality* refinement whose failure falls
#: back to the fused sparse+dense order instantly and losslessly, so waiting is
#: strictly worse than giving up: 5s is ~16x a healthy call (~300ms), and a
#: rerank slower than that cannot land inside the retrieval SLO regardless.
RERANK_TIMEOUT = httpx.Timeout(5.0, connect=_CONNECT)

#: **Background memory-file synthesis** (``copilot.worker.synthesizer``). The one
#: LLM call with no clinician waiting on it: the poller synthesizes off the
#: request path, so it is on none of the interactive SLOs and its budget is set
#: by generation size rather than by a latency target — 2048 ``max_tokens`` of
#: strict JSON, non-streaming. 30s is comfortably above a healthy generation
#: while still bounding a hang to seconds. It is not looser than
#: :data:`VISION_TIMEOUT` despite both being off-request, because it uploads no
#: images.
SYNTHESIS_TIMEOUT = httpx.Timeout(30.0, connect=_CONNECT)

#: **OAuth token endpoint** (``copilot.fhir.auth``). Unchanged from the 10s the
#: providers already used — it sits inside the chat turn's budget but is
#: overwhelmingly served from the in-memory cache, so it is not a hot path.
TOKEN_TIMEOUT = httpx.Timeout(10.0, connect=_CONNECT)

# --- Anthropic SDK retry bounds ---------------------------------------------
#
# The Anthropic SDK already implements exactly the retry policy this module
# describes -- bounded attempts, exponential backoff WITH jitter, honouring
# `retry-after`, retrying 408/409/429/5xx and connection errors, and never
# retrying any other 4xx. It was already correct and is left in place rather
# than wrapped in a second, competing retry loop (which would multiply out to
# attempts x attempts). These constants only make the bound EXPLICIT at each
# construction site, so the policy is a stated decision rather than an inherited
# default -- the same reason the timeouts above are now passed in.

#: Chat: the SDK default. Worst case 3 attempts x 20s = 60s of hang, versus the
#: 30 MINUTES the same three attempts could take at the inherited 600s read.
CHAT_MAX_RETRIES = 2

#: Vision: the SDK default. Ingestion is fail-closed, so exhausting retries marks
#: the document ``failed`` and the ingestion-failure alert (§7.2 Alert 5) surfaces it.
VISION_MAX_RETRIES = 2

#: Gating: one retry only (2 attempts). It runs inside a clinician's turn and
#: fails safe, so a single retry to ride out a transient 429 on the cheap gating
#: model is worth it and a second is not. Applies to both gating-kind calls: the
#: critic's safety pass and the optional entailment check.
GATING_MAX_RETRIES = 1

#: Background synthesis: the SDK default. Nobody is waiting, so the full budget
#: is worth spending to avoid a re-poll.
SYNTHESIS_MAX_RETRIES = 2


# --- retry policy -----------------------------------------------------------


@dataclass(frozen=True)
class RetryPolicy:
    """A bounded, jittered retry budget.

    ``attempts`` is the TOTAL number of calls, not the number of retries, so
    ``attempts=1`` means "no retry" and is exactly today's un-retried behaviour.
    """

    attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 2.0

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("RetryPolicy.attempts must be >= 1")
        if self.base_delay < 0.0 or self.max_delay < 0.0:
            raise ValueError("RetryPolicy delays must be >= 0")


#: Read-path default: 3 attempts, ~0.2s-2s of jittered backoff between them.
DEFAULT_RETRY = RetryPolicy()

#: Rerank: one retry only. It has the tightest SLO (800ms warn) *and* the
#: cheapest fallback (instant fused order), so one attempt to ride out a
#: transient blip is worth it and a second is not.
RERANK_RETRY = RetryPolicy(attempts=2, base_delay=0.1, max_delay=0.5)

#: Statuses worth re-sending an identical request for. 408 is literally "request
#: timeout"; 429 is "slow down"; 5xx is "my fault, not yours". Every other 4xx is
#: a verdict on the request itself and re-sending it changes nothing.
_RETRYABLE_STATUS = frozenset({408, 429})

#: Transport failures where the request may never have been served at all.
#: Deliberately NOT ``httpx.TransportError`` (its parent), which would also
#: sweep in ``UnsupportedProtocol`` / ``LocalProtocolError`` — programming
#: errors that will fail identically forever.
_RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError)


def is_retryable_status(status_code: int) -> bool:
    """True for 408/429/5xx — never for any other 4xx."""
    return status_code in _RETRYABLE_STATUS or status_code >= 500


def is_retryable_exception(exc: BaseException) -> bool:
    """True for timeouts and connection errors — the genuinely transient ones."""
    return isinstance(exc, _RETRYABLE_EXCEPTIONS)


def retryable_response(response: httpx.Response) -> bool:
    """``should_retry_result`` predicate for a raw :class:`httpx.Response`."""
    return is_retryable_status(response.status_code)


def backoff_delay(
    attempt: int, policy: RetryPolicy, rand: Callable[[], float] = random.random
) -> float:
    """Full-jitter exponential backoff for the (0-based) completed ``attempt``.

    Uniform in ``[0, min(max_delay, base_delay * 2**attempt)]`` — the "full
    jitter" variant from AWS's *Exponential Backoff and Jitter*. The
    randomisation is the whole point, not a garnish: without it, every client
    that failed on the same upstream blip retries at the identical instant and
    re-creates the outage it was backing off from (thundering herd). The
    exponential ceiling bounds the wait; the jitter spreads the herd.
    """
    # 2.0 ** attempt, not 2 ** attempt: the float literal keeps the expression
    # statically float (int ** int widens to Any under mypy).
    ceiling = min(policy.max_delay, policy.base_delay * (2.0**attempt))
    return rand() * ceiling


def retry_sync[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_RETRY,
    should_retry_result: Callable[[T], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Call ``fn`` until it succeeds, is non-retryable, or the budget runs out.

    The **final attempt is un-guarded on purpose**: its result is returned and
    its exception propagates untouched, so an exhausted retry is byte-for-byte
    the un-retried failure the caller already handles. That is what keeps every
    fail-safe downstream (the reranker's fused-order fallback, the critic's
    deterministic partition) intact — a retry can turn a failure into a success,
    but it can never turn a graceful degrade into a raised error.
    """
    for attempt in range(policy.attempts - 1):
        try:
            result = fn()
        except Exception as exc:
            if not is_retryable_exception(exc):
                raise
        else:
            if should_retry_result is None or not should_retry_result(result):
                return result
        sleep(backoff_delay(attempt, policy, rand))
    return fn()


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY,
    should_retry_result: Callable[[T], bool] | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Async twin of :func:`retry_sync` — identical policy and semantics."""
    for attempt in range(policy.attempts - 1):
        try:
            result = await fn()
        except Exception as exc:
            if not is_retryable_exception(exc):
                raise
        else:
            if should_retry_result is None or not should_retry_result(result):
                return result
        await sleep(backoff_delay(attempt, policy, rand))
    return await fn()
