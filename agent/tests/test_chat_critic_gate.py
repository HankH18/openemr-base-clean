"""The critic's verdict is CONSUMED — a rejected claim is not served.

The critic partitions the drafted claims into accepted/rejected; these tests pin
that the partition actually reaches the physician's answer, and that it can only
ever REMOVE evidence:

- a claim the critic rejected is not served (the verdict is not decorative);
- a claim the *verifier* rejected cannot be resurrected by an adversarial critic
  that accepts it (the deterministic gate stays authoritative, and the critic is
  applied by filtering the verifier's survivors — not by reading its accept list
  as a source of claims);
- an all-rejected verdict collapses into the pre-existing "no grounded claims →
  withheld" policy rather than a new state;
- the flag-OFF inline path never consults a critic at all.

Drives ``ChatService`` directly with the in-memory FHIR double from
``test_chat_routes``; the critic is injected by substituting the graph builder
the service calls, so the real ``AgentGraph`` (real supervisor, workers, and
deterministic verifier) runs with only the critic swapped.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.chat.service import _WITHHELD_ANSWER, ChatReply, ChatService
from copilot.config import Settings, get_settings
from copilot.domain.primitives import ClinicianId, PatientId
from copilot.graph.contracts import CriticVerdict
from copilot.graph.critic import ReviewableClaim
from tests.test_chat_routes import _COHORT, CLIN, SICK, _FakeFhir

# A question the stub agent answers from every resource in the cohort, so the
# turn drafts MULTIPLE claims — the only way to tell "the critic dropped one"
# apart from "the turn was withheld".
SUMMARY_Q = "Summarize everything for this patient"


# --- critic doubles ---------------------------------------------------------


class _PredicateCritic:
    """Rejects exactly the claim texts matching ``predicate``; accepts the rest."""

    def __init__(self, predicate: Callable[[str], bool]) -> None:
        self._predicate = predicate
        self.reviewed: list[list[str]] = []

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        texts = [str(getattr(c, "text", "")) for c in claims]
        self.reviewed.append(texts)
        return CriticVerdict(
            accepted=[t for t in texts if not self._predicate(t)],
            rejected=[t for t in texts if self._predicate(t)],
        )


class _ResurrectingCritic:
    """Adversarial: accepts EVERY drafted claim, including verifier-rejected ones.

    Models the worst case the ordering must survive — a critic (LLM-backed, and
    therefore capable of being wrong or manipulated) that tries to vouch for a
    claim the deterministic re-fetch already dropped.
    """

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        return CriticVerdict(accepted=[str(getattr(c, "text", "")) for c in claims], rejected=[])


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def _db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Temp SQLite file + schema; keyless settings so every factory stubs."""
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "chat_critic.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("COPILOT_VOYAGE_API_KEY", "")
    monkeypatch.setenv("COPILOT_COHERE_API_KEY", "")
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield str(db_file)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _reader(
    read_cohort: dict[str, dict[str, list[dict[str, Any]]]] | None = None,
) -> Callable[[], Any]:
    """A per-turn FHIR reader factory over the cohort double.

    ``read_cohort`` is the (optionally drifted) copy the verifier's live re-fetch
    sees, which is how a test makes the deterministic gate reject a claim.
    """

    def _make() -> _FakeFhir:
        return _FakeFhir(_COHORT, read_cohort=read_cohort)

    return _make


def _inject_critic(
    monkeypatch: pytest.MonkeyPatch, critic: Any, fhir: Callable[[], Any]
) -> list[str]:
    """Make the service build a real graph whose ONLY substitution is ``critic``.

    Returns a list that records each ``build_graph`` call, so a test can prove the
    graph (and therefore the critic) was never reached at all.
    """
    from copilot.graph.evidence_retriever import build_evidence_retriever
    from copilot.graph.intake_extractor import build_intake_extractor
    from copilot.graph.supervisor import AgentGraph, build_supervisor

    built: list[str] = []

    def _build(settings: Settings, **kwargs: Any) -> AgentGraph:
        built.append("graph")
        return AgentGraph(
            settings=settings,
            supervisor=build_supervisor(settings),
            intake_extractor=build_intake_extractor(settings),
            evidence_retriever=build_evidence_retriever(settings),
            critic=critic,
            observability=kwargs.get("observability"),
            max_iterations=kwargs.get("max_iterations"),
            fhir_client_factory=kwargs.get("fhir_client_factory") or fhir,
        )

    monkeypatch.setattr("copilot.chat.service.build_graph", _build)
    return built


def _service(*, graph_enabled: bool, fhir: Callable[[], Any]) -> ChatService:
    return ChatService(
        get_settings().model_copy(update={"chat_graph_enabled": graph_enabled}),
        fhir_client_factory=fhir,
    )


async def _chat(service: ChatService, message: str = SUMMARY_Q) -> ChatReply:
    return await service.chat(
        clinician_id=ClinicianId(value=CLIN),
        patient_id=PatientId(value=SICK),
        message=message,
        correlation_id="chat-critic-corr-01",
    )


def _texts(reply: ChatReply) -> str:
    return " | ".join(c.text for c in reply.claims).lower()


# --- tests ------------------------------------------------------------------


class TestCriticVerdictIsConsumed:
    async def test_baseline_summary_drafts_several_claims(self, _db: str) -> None:
        """Guard for the tests below: the fixture question must ground >1 claim.

        Without this, "the rejected claim is absent" would pass vacuously on a
        turn that grounded nothing in the first place.
        """
        reply = await _chat(_service(graph_enabled=True, fhir=_reader()))
        assert reply.action.value == "served"
        assert len(reply.claims) >= 2, f"expected a multi-claim draft, got {reply.claims}"
        assert "aspirin" in _texts(reply)

    async def test_critic_rejected_claim_withholds_the_whole_turn(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A critic rejection of a verifier-passed claim withholds the WHOLE turn.

        USER APPROVED option (a): escalate narrative_inconsistency + degraded to
        whole-turn withhold (2026-07-19). The prior contract (introduced by
        ``edbeb99 fix(chat): serve the critic's verdict``) narrowed the draft and
        served the survivors — but the served answer is the agent's whole PROSE,
        which still narrated the dropped claim (the fail-open this decision
        closes). The ``_PredicateCritic`` here models a NON-unsafe (narrative-
        inconsistency) demotion of a claim the verifier passed, so the whole turn
        is now withheld rather than served with the citation quietly stripped.
        """
        fhir = _reader()
        critic = _PredicateCritic(lambda text: "aspirin" in text.lower())
        _inject_critic(monkeypatch, critic, fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert critic.reviewed, "the graph must actually consult the critic"
        # Was: `assert reply.claims` (narrow-and-serve) + `"troponin" in _texts`.
        # Under option (a) a critic demotion of a verifier-passed claim withholds
        # the whole turn, reusing the same withheld path `unsafe_action` uses.
        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER
        # The rejected claim never reaches the physician — now because nothing does.
        assert "aspirin" not in _texts(reply)
        assert "troponin" not in _texts(reply)

    async def test_critic_cannot_resurrect_a_verifier_rejected_claim(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verifier drops the drifted troponin; a critic accepting it changes nothing.

        The live re-fetch reads 1.5 where the agent answered 0.9, so the
        deterministic gate rejects that claim. The injected critic then "accepts"
        every drafted claim — including that one. The verifier is authoritative
        and runs first, so the resurrection must fail.
        """
        import copy

        drifted = copy.deepcopy(_COHORT)
        drifted["1001"]["Observation"][0]["valueQuantity"]["value"] = 1.5
        fhir = _reader(read_cohort=drifted)
        _inject_critic(monkeypatch, _ResurrectingCritic(), fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert "troponin" not in _texts(reply), (
            "the critic must not be able to serve a claim the deterministic "
            f"verifier rejected — served claims: {[c.text for c in reply.claims]}"
        )
        # Was: `assert "aspirin" in _texts(reply)` (the surviving claim served).
        # USER APPROVED option (a): escalate narrative_inconsistency + degraded to
        # whole-turn withhold (2026-07-19). The drifted troponin makes this a
        # `degraded` verification, so the whole turn now withholds rather than
        # serving the surviving aspirin claim next to prose that could still
        # narrate the dropped troponin. The critic still cannot resurrect troponin
        # (asserted above); the difference is aspirin is no longer served either.
        assert reply.action.value == "withheld"
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER

    async def test_all_rejected_collapses_to_the_existing_withheld_policy(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A critic that rejects everything yields the SAME withheld reply as an
        ungroundable question — not a new state."""
        fhir = _reader()
        _inject_critic(monkeypatch, _PredicateCritic(lambda _text: True), fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER

    async def test_rejected_claim_leaves_no_audit_trail_entry(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dropped claim's resource is not recorded as returned to the clinician.

        The HIPAA access trail records what the answer actually cited; a claim the
        critic removed was never served, so its resource must not appear.
        """
        fhir = _reader()
        _inject_critic(monkeypatch, _PredicateCritic(lambda t: "aspirin" in t.lower()), fhir)

        await _chat(_service(graph_enabled=True, fhir=fhir))

        engine = sa.create_engine(f"sqlite:///{_db}")
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    sa.text("SELECT resources_returned FROM audit_log WHERE action = 'chat'")
                ).fetchall()
        finally:
            engine.dispose()
        assert rows, "a chat read must leave an audit row"
        recorded = " ".join(str(r[0]) for r in rows)
        assert "med-1001-asa" not in recorded, (
            f"the rejected claim's resource must not be logged as returned: {recorded}"
        )


class TestFlagOffIsUnaffected:
    async def test_flag_off_never_consults_the_critic(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default (flag OFF) path builds no graph, so no critic can gate it.

        Byte-identical: with a reject-EVERYTHING critic wired into the graph
        builder, the inline reply is still the full grounded answer — proving the
        critic is not in the default path at all.
        """
        fhir = _reader()
        critic = _PredicateCritic(lambda _text: True)
        built = _inject_critic(monkeypatch, critic, fhir)

        reply = await _chat(_service(graph_enabled=False, fhir=fhir))

        assert built == [], "the inline path must not build the graph"
        assert critic.reviewed == [], "the inline path must not consult the critic"
        assert reply.action.value == "served"
        assert "aspirin" in _texts(reply), "flag-OFF claims must be unnarrowed"

    async def test_flag_off_reply_is_identical_with_and_without_the_graph_wired(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Flag-OFF output is byte-identical to the same turn before the critic
        gate existed — compared field by field against an unpatched run."""
        fhir = _reader()
        clean = await _chat(_service(graph_enabled=False, fhir=fhir))

        _inject_critic(monkeypatch, _PredicateCritic(lambda _text: True), fhir)
        patched = await _chat(_service(graph_enabled=False, fhir=fhir))

        assert patched.answer == clean.answer
        assert [c.text for c in patched.claims] == [c.text for c in clean.claims]
        assert patched.action == clean.action
        assert patched.passed == clean.passed
        # The graph-only reply fields stay at their inline defaults: no evidence
        # retrieved by this path (the route owns that), and no handoffs.
        assert patched.guideline_evidence is None
        assert patched.handoffs == []
