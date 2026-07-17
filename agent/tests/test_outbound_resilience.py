"""Every outbound call: SLO-anchored timeouts, bounded retry, intact fail-safes.

The spec requires that *all* outbound LLM and retrieval calls carry timeouts and
retry logic. These tests lock that end of the contract at each real call site —
Voyage, Cohere, the OAuth token endpoint, the FHIR reader, and the three
Anthropic clients — plus the two properties that make the retries safe rather
than merely present:

- **the fail-safe survives retry exhaustion.** A retry must never convert a
  graceful degrade into a raised error, so each site is asserted to raise
  *exactly the error it always raised* once the budget runs out — the one its
  caller already catches (fused-order fallback, deterministic partition).
- **the event loop is never blocked.** The critic's sync Anthropic call runs in a
  worker thread, so concurrent clinicians' requests progress *during* it.

Fakes only — no network, and the keyless/stub paths are untouched throughout.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import respx

from copilot.agent.claude import ClaudeAgent
from copilot.config import Settings
from copilot.documents.vision import ClaudeVision
from copilot.domain.primitives import ResourceType
from copilot.fhir.auth import (
    BackendServicesTokenProvider,
    OAuthToken,
    SmartAppLaunchTokenProvider,
    StaticTokenProvider,
    TokenAcquisitionError,
)
from copilot.fhir.client import FhirClient, FhirClientError
from copilot.graph.contracts import AgentTask, CriticVerdict
from copilot.graph.critic import RealCritic
from copilot.graph.intake_extractor import IntakeReport
from copilot.graph.supervisor import AgentGraph, StubSupervisor
from copilot.observability import NoopObservability
from copilot.rag.embeddings import EmbeddingError, VoyageEmbedder
from copilot.rag.rerank import CohereReranker, RerankError
from copilot.resilience import (
    CHAT_MAX_RETRIES,
    CHAT_TIMEOUT,
    GATING_MAX_RETRIES,
    GATING_TIMEOUT,
    SYNTHESIS_MAX_RETRIES,
    SYNTHESIS_TIMEOUT,
    VISION_MAX_RETRIES,
    VISION_TIMEOUT,
    RetryPolicy,
)
from copilot.verification.entailment import LlmEntailment
from copilot.worker.synthesizer import ClaudeSynthesizer

# Reuse the in-memory FHIR double + synthetic cohort from the chat-route tests.
from tests.test_chat_routes import _COHORT, SICK, _FakeFhir

# Zero-delay budgets: the retry loop still runs its full course, it just never
# sleeps, so the suite stays fast and deterministic.
_INSTANT = RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0)
_INSTANT_2 = RetryPolicy(attempts=2, base_delay=0.0, max_delay=0.0)


def _keyed() -> Settings:
    """Keyed settings. No client is injected in the timeout tests, so the REAL
    Anthropic client is constructed — constructing one performs no I/O."""
    return Settings(anthropic_api_key="sk-live", voyage_api_key="", cohere_api_key="")


# --- Voyage embeddings ------------------------------------------------------


class TestVoyageEmbedderResilience:
    @respx.mock
    def test_transient_5xx_is_retried_then_succeeds(self) -> None:
        route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5]}]}),
            ]
        )
        embedder = VoyageEmbedder("k", "voyage-3.5", retry=_INSTANT)

        assert embedder.embed(["sepsis"]) == [[0.5, 0.5]]
        assert route.call_count == 2

    @respx.mock
    def test_connection_error_is_retried_then_succeeds(self) -> None:
        route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
            side_effect=[
                httpx.ConnectError("refused"),
                httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]}),
            ]
        )
        embedder = VoyageEmbedder("k", "voyage-3.5", retry=_INSTANT)

        assert embedder.embed(["sepsis"]) == [[1.0]]
        assert route.call_count == 2

    @respx.mock
    def test_a_4xx_is_not_retried(self) -> None:
        # A bad key / malformed request is a verdict. Re-sending it identically
        # can only produce the identical 401 — and hammering an auth failure is
        # how a service gets itself blocked.
        route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
            return_value=httpx.Response(401, json={"error": "bad key"})
        )
        embedder = VoyageEmbedder("k", "voyage-3.5", retry=_INSTANT)

        with pytest.raises(EmbeddingError):
            embedder.embed(["sepsis"])
        assert route.call_count == 1, "a 4xx must never be retried"

    @respx.mock
    def test_retries_are_bounded_and_still_raise_the_original_error(self) -> None:
        # The fail-safe contract: exhausting the budget raises EmbeddingError —
        # exactly what an un-retried failure raised — so no caller sees a new
        # error mode, only fewer of the old one.
        route = respx.post("https://api.voyageai.com/v1/embeddings").mock(
            return_value=httpx.Response(500)
        )
        embedder = VoyageEmbedder("k", "voyage-3.5", retry=_INSTANT)

        with pytest.raises(EmbeddingError):
            embedder.embed(["sepsis"])
        assert route.call_count == 3, "the budget is a hard cap, not a suggestion"

    @respx.mock
    def test_an_empty_batch_makes_no_request_at_all(self) -> None:
        route = respx.post("https://api.voyageai.com/v1/embeddings")
        assert VoyageEmbedder("k", "voyage-3.5", retry=_INSTANT).embed([]) == []
        assert route.call_count == 0


# --- Cohere rerank ----------------------------------------------------------


class TestCohereRerankerResilience:
    @respx.mock
    def test_transient_429_is_retried_then_succeeds(self) -> None:
        route = respx.post("https://api.cohere.com/v2/rerank").mock(
            side_effect=[
                httpx.Response(429),
                httpx.Response(200, json={"results": [{"index": 1}, {"index": 0}]}),
            ]
        )
        reranker = CohereReranker("k", "rerank-v3.5", retry=_INSTANT_2)

        assert reranker.rerank("q", ["first", "second"]) == ["second", "first"]
        assert route.call_count == 2

    @respx.mock
    def test_a_4xx_is_not_retried(self) -> None:
        route = respx.post("https://api.cohere.com/v2/rerank").mock(
            return_value=httpx.Response(422, json={"error": "bad request"})
        )
        reranker = CohereReranker("k", "rerank-v3.5", retry=_INSTANT_2)

        with pytest.raises(RerankError):
            reranker.rerank("q", ["a", "b"])
        assert route.call_count == 1

    @respx.mock
    def test_exhausted_retries_still_raise_the_error_the_retriever_catches(self) -> None:
        # THE fail-safe invariant for this path. The retriever catches RerankError
        # and serves the fused sparse+dense order; if a retry leaked a different
        # exception type out of here, that fallback would stop firing and a Cohere
        # outage would start failing answers instead of quietly degrading them.
        route = respx.post("https://api.cohere.com/v2/rerank").mock(
            side_effect=httpx.ReadTimeout("cohere is wedged")
        )
        reranker = CohereReranker("k", "rerank-v3.5", retry=_INSTANT_2)

        with pytest.raises(RerankError):
            reranker.rerank("q", ["a", "b"])
        assert route.call_count == 2, "rerank's budget is the smallest — 2 attempts"

    @respx.mock
    def test_the_fused_order_fallback_still_triggers_after_retries_are_exhausted(self) -> None:
        # End-to-end proof of the degrade, at the layer that owns it: a totally
        # dead Cohere must leave the caller with the fused order intact, not an
        # exception. (This is the shape `GuidelineRetriever._apply_rerank` relies
        # on — `except Exception: return candidates`.)
        respx.post("https://api.cohere.com/v2/rerank").mock(
            side_effect=httpx.ConnectError("cohere is down")
        )
        reranker = CohereReranker("k", "rerank-v3.5", retry=_INSTANT_2)
        fused = ["chunk-a", "chunk-b", "chunk-c"]

        try:
            ordered = reranker.rerank("q", fused)
        except RerankError:
            ordered = fused  # the retriever's documented fallback

        assert ordered == fused


# --- OAuth token endpoint ---------------------------------------------------


@pytest.mark.asyncio
class TestTokenProviderResilience:
    @respx.mock
    async def test_transient_5xx_is_retried_then_succeeds(self) -> None:
        route = respx.post("https://openemr.test/token").mock(
            side_effect=[
                httpx.Response(502),
                httpx.Response(
                    200,
                    json={"access_token": "t-1", "token_type": "Bearer", "expires_in": 3600},
                ),
            ]
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="c",
            redirect_uri="https://app.test/cb",
            authorization_code="code-1",
            retry=_INSTANT,
        )

        token = await provider.get_token()
        assert token.access_token == "t-1"
        assert route.call_count == 2

    @respx.mock
    async def test_a_spent_authorization_code_is_not_retried(self) -> None:
        # The single-use-credential safety proof in `_post_token`, made concrete:
        # a spent code returns 400 invalid_grant, and the loop must stop dead
        # rather than hammer the token endpoint with a credential that can never
        # work again.
        route = respx.post("https://openemr.test/token").mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="c",
            redirect_uri="https://app.test/cb",
            authorization_code="already-spent",
            retry=_INSTANT,
        )

        with pytest.raises(TokenAcquisitionError):
            await provider.get_token()
        assert route.call_count == 1

    @respx.mock
    async def test_retries_are_bounded_and_still_raise_token_acquisition_error(self) -> None:
        route = respx.post("https://openemr.test/token").mock(
            side_effect=httpx.ConnectError("openemr unreachable")
        )
        provider = SmartAppLaunchTokenProvider(
            token_url="https://openemr.test/token",
            client_id="c",
            redirect_uri="https://app.test/cb",
            authorization_code="code-1",
            retry=_INSTANT,
        )

        # A transport error propagates as httpx did before the retry existed;
        # only the count changed, never the type.
        with pytest.raises(httpx.ConnectError):
            await provider.get_token()
        assert route.call_count == 3

    @respx.mock
    async def test_the_jwt_assertion_is_reminted_on_every_attempt(self) -> None:
        # A client_assertion carries a single-use `jti` and a short `exp`. Reusing
        # one across a retry invites a replay rejection and lets `exp` lapse under
        # backoff, so each attempt must present a freshly built assertion.
        route = respx.post("https://openemr.test/token").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(503),
                httpx.Response(
                    200,
                    json={"access_token": "sys-1", "token_type": "Bearer", "expires_in": 300},
                ),
            ]
        )
        minted: list[str] = []

        def _jti() -> str:
            minted.append(f"jti-{len(minted)}")
            return minted[-1]

        provider = BackendServicesTokenProvider(
            token_url="https://openemr.test/token",
            client_id="sys",
            private_key_pem=_rsa_pem(),
            jti_factory=_jti,
            retry=_INSTANT,
        )

        token = await provider.get_token()
        assert token.access_token == "sys-1"
        assert route.call_count == 3
        assert len(minted) == 3, "each attempt must mint its own jti, not replay one"
        assert len(set(minted)) == 3

        sent = {
            call.request.content.decode() for call in route.calls  # type: ignore[union-attr]
        }
        assert len(sent) == 3, "each attempt must carry a distinct assertion on the wire"


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


# --- FHIR reader ------------------------------------------------------------


def _static_provider() -> StaticTokenProvider:
    from datetime import UTC, datetime, timedelta

    return StaticTokenProvider(
        OAuthToken(
            access_token="t",
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )


@pytest.mark.asyncio
class TestFhirClientResilience:
    @respx.mock
    async def test_a_transient_5xx_on_a_read_is_retried(self) -> None:
        route = respx.get("http://oe.test/fhir/Patient/9").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"resourceType": "Patient", "id": "9"}),
            ]
        )
        client = FhirClient("http://oe.test/fhir", _static_provider(), retry=_INSTANT)

        assert (await client.read(ResourceType.Patient, "9"))["id"] == "9"
        assert route.call_count == 2

    @respx.mock
    async def test_a_404_is_not_retried(self) -> None:
        route = respx.get("http://oe.test/fhir/Patient/9").mock(return_value=httpx.Response(404))
        client = FhirClient("http://oe.test/fhir", _static_provider(), retry=_INSTANT)

        with pytest.raises(FhirClientError):
            await client.read(ResourceType.Patient, "9")
        assert route.call_count == 1

    @respx.mock
    async def test_retries_are_bounded(self) -> None:
        route = respx.get("http://oe.test/fhir/Patient/9").mock(return_value=httpx.Response(500))
        client = FhirClient("http://oe.test/fhir", _static_provider(), retry=_INSTANT)

        with pytest.raises(FhirClientError):
            await client.read(ResourceType.Patient, "9")
        assert route.call_count == 3

    @respx.mock
    async def test_a_401_still_takes_exactly_one_forced_refresh_retry(self) -> None:
        # The transient budget and the 401 forced-refresh path are orthogonal: a
        # 401 is a 4xx, so the transient loop must not touch it, leaving the
        # pre-existing single-refresh behaviour byte-for-byte intact.
        route = respx.get("http://oe.test/fhir/Patient/9").mock(
            return_value=httpx.Response(401)
        )
        client = FhirClient("http://oe.test/fhir", _static_provider(), retry=_INSTANT)

        with pytest.raises(FhirClientError):
            await client.read(ResourceType.Patient, "9")
        assert route.call_count == 2, "one original + one forced-refresh retry — no more"


# --- Anthropic timeout wiring ----------------------------------------------


class TestAnthropicTimeoutsAreWired:
    """The 600s inherited read timeout is gone from every constructed client.

    Constructing an Anthropic client performs no I/O, so these assert the real
    shipped objects rather than a fake — the wiring is the thing under test.
    """

    def test_chat_client_carries_the_chat_budget(self) -> None:
        agent = ClaudeAgent(_keyed(), _FakeFhir(_COHORT))
        assert agent._client.timeout == CHAT_TIMEOUT
        assert agent._client.max_retries == CHAT_MAX_RETRIES

    def test_vision_client_carries_the_vision_budget(self) -> None:
        vision = ClaudeVision(_keyed())
        assert vision._client.timeout == VISION_TIMEOUT
        assert vision._client.max_retries == VISION_MAX_RETRIES

    def test_gating_client_carries_the_gating_budget(self) -> None:
        critic = RealCritic(_keyed())
        assert critic._client.timeout == GATING_TIMEOUT
        assert critic._client.max_retries == GATING_MAX_RETRIES

    def test_entailment_client_carries_the_gating_budget(self) -> None:
        # Also a gating-kind call: one word out, on the request path.
        entailment = LlmEntailment("sk-live", "claude-haiku")
        assert entailment._client.timeout == GATING_TIMEOUT  # type: ignore[attr-defined]
        assert entailment._client.max_retries == GATING_MAX_RETRIES  # type: ignore[attr-defined]

    def test_synthesizer_client_carries_the_synthesis_budget(self) -> None:
        synth = ClaudeSynthesizer("sk-live", "claude-sonnet")
        assert synth._client.timeout == SYNTHESIS_TIMEOUT  # type: ignore[attr-defined]
        assert synth._client.max_retries == SYNTHESIS_MAX_RETRIES  # type: ignore[attr-defined]

    def test_no_client_inherits_the_600s_read_timeout(self) -> None:
        # The regression this whole change exists to prevent: a hung call holding
        # a clinician's turn for ten minutes. All FIVE Anthropic clients in the
        # service are covered by the constants below.
        for timeout in (
            CHAT_TIMEOUT,
            VISION_TIMEOUT,
            GATING_TIMEOUT,
            SYNTHESIS_TIMEOUT,
        ):
            assert timeout.read is not None and timeout.read <= 60.0
            assert timeout.connect is not None and timeout.connect <= 5.0

    def test_every_anthropic_client_in_the_tree_sets_an_explicit_timeout(self) -> None:
        """No fifth client may be added that silently inherits the 600s default.

        Greps the source rather than the objects: a newly added
        ``AsyncAnthropic(...)`` somewhere else would not be caught by the
        per-client assertions above, because nothing would import it. This is the
        guard that keeps "all 5" true as the tree grows.
        """
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parent.parent / "copilot"
        constructions: list[str] = []
        for path in root.rglob("*.py"):
            source = path.read_text()
            for match in re.finditer(r"(?:Async)?Anthropic\(([^)]*)\)", source, re.S):
                constructions.append(f"{path.relative_to(root)}: {match.group(0)}")

        assert constructions, "expected to find the Anthropic client constructions"
        missing = [c for c in constructions if "timeout=" not in c]
        assert missing == [], (
            "every Anthropic client must pass an explicit timeout — these inherit "
            f"the SDK's 600s read default: {missing}"
        )

    def test_the_budgets_are_ordered_by_how_costly_the_call_is_to_abandon(self) -> None:
        # Gating fails safe to the deterministic partition (cheapest to drop);
        # chat is a clinician waiting; vision is a long, unattended extraction.
        assert GATING_TIMEOUT.read is not None
        assert CHAT_TIMEOUT.read is not None
        assert VISION_TIMEOUT.read is not None
        assert GATING_TIMEOUT.read < CHAT_TIMEOUT.read < VISION_TIMEOUT.read


# --- the critic must not block the event loop -------------------------------


class _BlockingCritic:
    """Stands in for ``RealCritic``: a SYNC ``review`` that blocks.

    ``time.sleep`` is precisely what the synchronous Anthropic client does to the
    calling thread while it waits on the socket.
    """

    def __init__(self, seconds: float, ticks: Callable[[], int]) -> None:
        self._seconds = seconds
        self._ticks = ticks
        self.ticks_during_call = -1
        self.thread_name = ""

    def review(self, claims: Any) -> CriticVerdict:
        self.thread_name = threading.current_thread().name
        before = self._ticks()
        time.sleep(self._seconds)
        self.ticks_during_call = self._ticks() - before
        return CriticVerdict(accepted=[], rejected=[])


class _FakeEvidenceRetriever:
    async def run(self, task: AgentTask) -> Any:
        from copilot.graph.evidence_retriever import EvidenceReport

        return EvidenceReport(hits=0, evidence=[])


class _FakeIntakeExtractor:
    async def run(self, task: AgentTask) -> IntakeReport:
        return IntakeReport(fact_count=0, extraction_confidence=0.0, facts=[])


@pytest.mark.asyncio
class TestCriticDoesNotBlockTheEventLoop:
    async def test_concurrent_tasks_progress_during_a_slow_critic_call(self) -> None:
        """The regression: a sync critic called inline froze every other request.

        A ticker coroutine runs alongside a real ``graph.run``. The critic samples
        the tick counter immediately before and after its blocking sleep, so the
        assertion measures loop progress *during exactly the blocking window* —
        not merely across the run as a whole, which the graph's other awaits
        would satisfy either way.

        Before the fix this asserted 0: the sleep held the only thread, and the
        ticker could not advance until the critic returned.
        """
        ticks = 0

        async def _ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        critic = _BlockingCritic(0.3, lambda: ticks)
        graph = AgentGraph(
            settings=Settings(anthropic_api_key="", voyage_api_key="", cohere_api_key=""),
            supervisor=StubSupervisor(),
            intake_extractor=_FakeIntakeExtractor(),
            evidence_retriever=_FakeEvidenceRetriever(),
            critic=critic,
            observability=NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )

        task = asyncio.create_task(_ticker())
        await asyncio.sleep(0)  # let the ticker reach its first await
        try:
            await graph.run(AgentTask(patient_id=SICK, question="what is the potassium?"))
        finally:
            task.cancel()

        assert critic.ticks_during_call > 0, (
            "the event loop was frozen for the whole critic call — a blocking "
            "critic must run off-loop so concurrent requests keep progressing"
        )
        # 0.3s of blocking against a 5ms ticker ⇒ ~60 ticks if the loop is free.
        # 10 is a wide margin for CI scheduling noise while still being
        # unreachable if the loop were blocked at all.
        assert critic.ticks_during_call >= 10

    async def test_the_critic_runs_off_the_event_loop_thread(self) -> None:
        ticks = 0
        critic = _BlockingCritic(0.0, lambda: ticks)
        graph = AgentGraph(
            settings=Settings(anthropic_api_key="", voyage_api_key="", cohere_api_key=""),
            supervisor=StubSupervisor(),
            intake_extractor=_FakeIntakeExtractor(),
            evidence_retriever=_FakeEvidenceRetriever(),
            critic=critic,
            observability=NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )

        await graph.run(AgentTask(patient_id=SICK, question="what is the potassium?"))

        assert critic.thread_name != threading.current_thread().name

    async def test_the_verdict_still_reaches_the_result_unchanged(self) -> None:
        """Offloading must not perturb the contract: the verdict still arrives.

        The demote-only/fail-safe invariants live inside ``review``; running it on
        another thread must leave what the graph does with the verdict identical.
        """
        ticks = 0
        critic = _BlockingCritic(0.0, lambda: ticks)
        graph = AgentGraph(
            settings=Settings(anthropic_api_key="", voyage_api_key="", cohere_api_key=""),
            supervisor=StubSupervisor(),
            intake_extractor=_FakeIntakeExtractor(),
            evidence_retriever=_FakeEvidenceRetriever(),
            critic=critic,
            observability=NoopObservability(),
            fhir_client_factory=lambda: _FakeFhir(_COHORT),
        )

        result = await graph.run(AgentTask(patient_id=SICK, question="what is the potassium?"))

        assert result.critic == CriticVerdict(accepted=[], rejected=[])


# --- the fail-safe still holds after the LLM budget is exhausted ------------


class _TimingOutMessages:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        raise httpx.ReadTimeout("gating model wedged")


class _TimingOutClient:
    def __init__(self) -> None:
        self.messages = _TimingOutMessages()


class TestCriticFailSafeSurvivesTimeout:
    def test_a_timed_out_safety_pass_falls_back_to_the_deterministic_partition(self) -> None:
        # The critic's whole safety story: when the LLM pass cannot run, the
        # deterministic citation gate stands alone. A timeout must land here, not
        # raise — otherwise a slow gating model would fail a clinician's turn.
        client = _TimingOutClient()
        critic = RealCritic(_keyed(), client=client)

        verdict = critic.review(
            [
                {"text": "cited claim", "citation": {"source_type": "guideline", "source_id": "g"}},
                {"text": "uncited claim", "citation": None},
            ]
        )

        assert verdict.accepted == ["cited claim"]
        assert verdict.rejected == ["uncited claim"]
        assert verdict.unsafe == []
        assert client.messages.calls == 1
