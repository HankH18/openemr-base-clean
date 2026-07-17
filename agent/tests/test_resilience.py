"""The shared retry/timeout policy — bounds, jitter, and what is NOT retried.

These lock the properties the resilience helper exists to guarantee, all with
fakes and zero network:

(a) a transient failure is retried and then succeeds;
(b) a 4xx is NEVER retried (it is a verdict, not a blip);
(c) retries are bounded — the budget is a hard cap, not a suggestion;
(d) backoff is jittered and bounded (no thundering herd);
(e) an exhausted budget re-raises the ORIGINAL failure untouched, so callers'
    fail-safe fallbacks still see exactly the error they already handle.

Every test drives a zero-delay :class:`RetryPolicy`, so the suite never sleeps.
:func:`backoff_delay` — the only thing that would sleep — is tested directly as
the pure function it is.
"""

from __future__ import annotations

import httpx
import pytest

from copilot.resilience import (
    DEFAULT_RETRY,
    RERANK_RETRY,
    RetryPolicy,
    backoff_delay,
    is_retryable_exception,
    is_retryable_status,
    retry_async,
    retry_sync,
    retryable_response,
)

# Zero delay ⇒ the retry loop still sleeps, but for 0.0s. No wall-clock cost.
_INSTANT = RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0)


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://example.test"))


class _Calls:
    """Records how many times a fake was invoked — the bound under test."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.count = 0

    def __call__(self) -> object:
        outcome = self._outcomes[min(self.count, len(self._outcomes) - 1)]
        self.count += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# --- classification ---------------------------------------------------------


class TestClassification:
    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_transient_statuses_are_retryable(self, status: int) -> None:
        assert is_retryable_status(status) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 200, 201])
    def test_client_errors_and_successes_are_not_retryable(self, status: int) -> None:
        # The 4xx family is a deterministic verdict on the request: re-sending it
        # unchanged can only produce the identical 4xx. 409 is included
        # deliberately — the Anthropic SDK retries it, our own policy does not.
        assert is_retryable_status(status) is False

    def test_timeouts_and_connection_errors_are_retryable(self) -> None:
        assert is_retryable_exception(httpx.ReadTimeout("slow")) is True
        assert is_retryable_exception(httpx.ConnectTimeout("slow")) is True
        assert is_retryable_exception(httpx.ConnectError("refused")) is True

    def test_non_transport_failures_are_not_retryable(self) -> None:
        # A bug in our own code must fail fast and loudly, never be re-run 3x.
        assert is_retryable_exception(ValueError("bug")) is False
        assert is_retryable_exception(httpx.UnsupportedProtocol("gopher://")) is False

    def test_status_error_from_raise_for_status_is_not_swept_in(self) -> None:
        # HTTPStatusError is an HTTPError but NOT a transport error: status-based
        # retry goes through should_retry_result, which never retries a 4xx.
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "https://x.test"), response=_response(404)
        )
        assert is_retryable_exception(exc) is False


# --- backoff / jitter -------------------------------------------------------


class TestBackoff:
    def test_delay_is_bounded_by_the_exponential_ceiling(self) -> None:
        policy = RetryPolicy(attempts=5, base_delay=0.2, max_delay=2.0)
        # rand() == 1.0 is the worst case: the full ceiling.
        assert backoff_delay(0, policy, rand=lambda: 1.0) == pytest.approx(0.2)
        assert backoff_delay(1, policy, rand=lambda: 1.0) == pytest.approx(0.4)
        assert backoff_delay(2, policy, rand=lambda: 1.0) == pytest.approx(0.8)

    def test_delay_never_exceeds_max_delay(self) -> None:
        policy = RetryPolicy(attempts=20, base_delay=0.2, max_delay=2.0)
        # Exponential growth is capped, so a long budget cannot back off forever.
        for attempt in range(20):
            assert backoff_delay(attempt, policy, rand=lambda: 1.0) <= 2.0

    def test_delay_is_jittered_not_fixed(self) -> None:
        # The anti-thundering-herd property: the SAME attempt number must not
        # always produce the same delay, or every client retries in lockstep.
        policy = RetryPolicy(attempts=3, base_delay=1.0, max_delay=10.0)
        assert backoff_delay(0, policy, rand=lambda: 0.0) == pytest.approx(0.0)
        assert backoff_delay(0, policy, rand=lambda: 0.5) == pytest.approx(0.5)
        assert backoff_delay(0, policy, rand=lambda: 1.0) == pytest.approx(1.0)

    def test_policy_rejects_a_zero_attempt_budget(self) -> None:
        with pytest.raises(ValueError, match="attempts"):
            RetryPolicy(attempts=0)


# --- retry_sync -------------------------------------------------------------


class TestRetrySync:
    def test_transient_exception_is_retried_then_succeeds(self) -> None:
        fake = _Calls(httpx.ConnectError("boom"), "ok")
        assert retry_sync(fake, policy=_INSTANT, sleep=lambda _s: None) == "ok"
        assert fake.count == 2

    def test_transient_status_is_retried_then_succeeds(self) -> None:
        fake = _Calls(_response(503), _response(200))
        result = retry_sync(
            fake,  # type: ignore[arg-type]
            policy=_INSTANT,
            should_retry_result=retryable_response,  # type: ignore[arg-type]
            sleep=lambda _s: None,
        )
        assert result.status_code == 200  # type: ignore[attr-defined]
        assert fake.count == 2

    def test_a_4xx_is_never_retried(self) -> None:
        fake = _Calls(_response(400))
        result = retry_sync(
            fake,  # type: ignore[arg-type]
            policy=_INSTANT,
            should_retry_result=retryable_response,  # type: ignore[arg-type]
            sleep=lambda _s: None,
        )
        assert result.status_code == 400  # type: ignore[attr-defined]
        assert fake.count == 1, "a 4xx is a verdict — re-sending it is pure waste"

    def test_a_non_transient_exception_is_never_retried(self) -> None:
        fake = _Calls(ValueError("bug in our own code"))
        with pytest.raises(ValueError, match="bug"):
            retry_sync(fake, policy=_INSTANT, sleep=lambda _s: None)
        assert fake.count == 1

    def test_retries_are_bounded_and_the_last_failure_propagates(self) -> None:
        fake = _Calls(httpx.ReadTimeout("always down"))
        with pytest.raises(httpx.ReadTimeout):
            retry_sync(fake, policy=_INSTANT, sleep=lambda _s: None)
        # Exactly the budget — never "keep going until it works".
        assert fake.count == 3

    def test_attempts_of_one_is_exactly_the_un_retried_behaviour(self) -> None:
        fake = _Calls(httpx.ReadTimeout("down"))
        with pytest.raises(httpx.ReadTimeout):
            retry_sync(fake, policy=RetryPolicy(attempts=1), sleep=lambda _s: None)
        assert fake.count == 1

    def test_a_persistently_transient_status_returns_the_last_response(self) -> None:
        # Exhaustion must hand the caller the real response, so its existing
        # raise_for_status/error mapping runs unchanged.
        fake = _Calls(_response(503))
        result = retry_sync(
            fake,  # type: ignore[arg-type]
            policy=_INSTANT,
            should_retry_result=retryable_response,  # type: ignore[arg-type]
            sleep=lambda _s: None,
        )
        assert result.status_code == 503  # type: ignore[attr-defined]
        assert fake.count == 3

    def test_backoff_is_slept_between_attempts_not_after_the_last(self) -> None:
        slept: list[float] = []
        fake = _Calls(httpx.ConnectError("x"), httpx.ConnectError("x"), _response(200))
        retry_sync(
            fake,  # type: ignore[arg-type]
            policy=_INSTANT,
            should_retry_result=retryable_response,  # type: ignore[arg-type]
            sleep=slept.append,
        )
        # 3 attempts ⇒ 2 gaps. A trailing sleep would delay the caller for nothing.
        assert len(slept) == 2


# --- retry_async ------------------------------------------------------------


@pytest.mark.asyncio
class TestRetryAsync:
    async def test_transient_exception_is_retried_then_succeeds(self) -> None:
        fake = _Calls(httpx.ConnectError("boom"), "ok")

        async def _fn() -> object:
            return fake()

        async def _sleep(_seconds: float) -> None:
            return None

        assert await retry_async(_fn, policy=_INSTANT, sleep=_sleep) == "ok"
        assert fake.count == 2

    async def test_a_4xx_is_never_retried(self) -> None:
        fake = _Calls(_response(401))

        async def _fn() -> httpx.Response:
            return fake()  # type: ignore[return-value]

        async def _sleep(_seconds: float) -> None:
            return None

        result = await retry_async(
            _fn, policy=_INSTANT, should_retry_result=retryable_response, sleep=_sleep
        )
        assert result.status_code == 401
        assert fake.count == 1

    async def test_retries_are_bounded(self) -> None:
        fake = _Calls(httpx.ReadTimeout("always down"))

        async def _fn() -> object:
            return fake()

        async def _sleep(_seconds: float) -> None:
            return None

        with pytest.raises(httpx.ReadTimeout):
            await retry_async(_fn, policy=_INSTANT, sleep=_sleep)
        assert fake.count == 3


# --- the shipped budgets ----------------------------------------------------


class TestShippedPolicies:
    def test_default_read_budget_is_bounded_and_jittered(self) -> None:
        assert DEFAULT_RETRY.attempts == 3
        assert DEFAULT_RETRY.max_delay > 0.0

    def test_rerank_budget_is_the_smallest(self) -> None:
        # Rerank has the tightest SLO (800ms warn) and the cheapest fallback
        # (instant fused order), so it must not spend a clinician's latency on a
        # third attempt. See copilot.resilience.RERANK_RETRY.
        assert RERANK_RETRY.attempts == 2
        assert RERANK_RETRY.attempts < DEFAULT_RETRY.attempts
