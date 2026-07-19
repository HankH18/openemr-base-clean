"""Serve-time PROSE is gated, not just the citation chips (option (a)).

The deterministic verifier + critic gate the CLAIMS a turn exposes, but the
free-form answer PROSE the physician reads used to be served unchanged even when
a claim behind it had been demoted. Two fail-open cases produced prose that
asserts a dropped claim ("no chest pain" over a "chest pain" record):

- a verifier ``degraded`` verdict — some claims passed, some failed, but the
  whole agent answer was served with only the survivors as chips; and
- a critic ``narrative_inconsistency`` flag — the critic dropped a verifier-
  passed claim from the evidence while the sentence stayed in the prose.

USER APPROVED option (a): escalate BOTH to a whole-turn withhold, reusing the
same withheld-answer path ``unsafe_action`` already used (2026-07-19). These
tests pin that escalation on BOTH serve paths (inline + graph), and guard the
two behaviours that must NOT change: a fully-grounded turn still serves, and an
``unsafe_action`` turn still withholds.

Drives ``ChatService`` directly with the in-memory FHIR double from
``test_chat_routes`` and the shared graph-wiring helpers from
``test_chat_critic_gate`` (the real ``AgentGraph`` runs with only the critic
swapped, so the deterministic verifier is genuinely in the path).
"""

from __future__ import annotations

import copy
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import sqlalchemy as sa

from copilot.chat.service import _WITHHELD_ANSWER
from copilot.config import get_settings
from copilot.graph.contracts import CriticVerdict
from copilot.graph.critic import ReviewableClaim, StubCritic
from tests.test_chat_critic_gate import (
    SUMMARY_Q,
    _chat,
    _inject_critic,
    _reader,
    _service,
    _texts,
)
from tests.test_chat_routes import _COHORT


@pytest.fixture
def _db(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Temp SQLite file + schema; keyless settings so every factory stubs.

    A local copy of ``test_chat_critic_gate``'s fixture (the codebase pattern is
    per-file db fixtures) rather than importing that module's ``_db``, which would
    shadow the fixture parameter name in every test below (ruff F811).
    """
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "prose_fail_open.db"
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


# --- critic doubles ---------------------------------------------------------


class _NarrativeInconsistencyCritic:
    """Rejects claims matching ``predicate`` — as narrative_inconsistency, NOT unsafe.

    Models the keyed ``RealCritic``'s ``narrative_inconsistency`` demotion: the
    matched claim lands in ``rejected`` but never in ``unsafe``. A verifier-passed
    claim is cited, so this is the exact keyed-safety-pass demotion the serve
    layer must now escalate to a whole-turn withhold.
    """

    def __init__(self, predicate: Any) -> None:
        self._predicate = predicate
        self.reviewed: list[list[str]] = []

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        texts = [str(getattr(c, "text", "")) for c in claims]
        self.reviewed.append(texts)
        return CriticVerdict(
            accepted=[t for t in texts if not self._predicate(t)],
            rejected=[t for t in texts if self._predicate(t)],
        )


class _UnsafeCritic:
    """Rejects claims matching ``predicate`` and marks them ``unsafe_action``."""

    def __init__(self, predicate: Any) -> None:
        self._predicate = predicate
        self.reviewed: list[list[str]] = []

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        texts = [str(getattr(c, "text", "")) for c in claims]
        self.reviewed.append(texts)
        rejected = [t for t in texts if self._predicate(t)]
        return CriticVerdict(
            accepted=[t for t in texts if not self._predicate(t)],
            rejected=rejected,
            unsafe=rejected,
        )


def _drifted_reader() -> Any:
    """A reader whose live re-fetch drifts the troponin value.

    The agent grounds troponin at 0.9 (the search cohort); the re-fetch reads
    1.5, so that one claim fails the value-match gate while aspirin and the
    NSTEMI condition still pass — a ``degraded`` verification.
    """
    drifted = copy.deepcopy(_COHORT)
    drifted["1001"]["Observation"][0]["valueQuantity"]["value"] = 1.5
    return _reader(read_cohort=drifted)


# --- (1) critic narrative_inconsistency -> whole turn withheld (graph) -------


class TestNarrativeInconsistencyWithholds:
    async def test_graph_narrative_inconsistency_withholds_whole_turn(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A NON-unsafe critic demotion of a verifier-passed claim withholds all.

        RED before option (a): the draft narrowed and the survivors served.
        GREEN after: the whole turn is withheld and the prose is the withheld
        message, so no sentence can assert the dropped claim.
        """
        fhir = _reader()  # no drift -> the verifier passes every claim
        critic = _NarrativeInconsistencyCritic(lambda t: "aspirin" in t.lower())
        _inject_critic(monkeypatch, critic, fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert critic.reviewed, "the graph must actually consult the critic"
        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER, "the served prose must be the withheld message"


# --- (2) verifier degraded -> whole turn withheld (inline AND graph) ---------


class TestDegradedWithholds:
    async def test_inline_degraded_verification_withholds_whole_turn(self, _db: str) -> None:
        """Inline path: a drifted claim degrades the verdict; the whole turn withholds.

        RED before: ``action == degraded`` and the surviving claims served next to
        the agent's full prose. GREEN after: withheld.
        """
        fhir = _drifted_reader()

        reply = await _chat(_service(graph_enabled=False, fhir=fhir))

        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER

    async def test_graph_degraded_verification_withholds_whole_turn(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Graph path (deterministic StubCritic): the same drift withholds the turn.

        The critic accepts every cited claim (no demotion), so this bites purely
        on the ``degraded`` escalation — not on critic narrowing.
        """
        fhir = _drifted_reader()
        _inject_critic(monkeypatch, StubCritic(), fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER


# --- (3) fully-grounded turn still serves (regression guard, both paths) -----


class TestFullyGroundedStillServes:
    async def test_inline_fully_grounded_turn_still_served(self, _db: str) -> None:
        fhir = _reader()  # no drift -> every claim verifies

        reply = await _chat(_service(graph_enabled=False, fhir=fhir))

        assert reply.action.value == "served"
        assert reply.passed is True
        assert len(reply.claims) >= 2, f"expected a multi-claim serve, got {reply.claims}"
        assert "aspirin" in _texts(reply)
        assert reply.answer != _WITHHELD_ANSWER

    async def test_graph_fully_grounded_turn_still_served(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fhir = _reader()
        _inject_critic(monkeypatch, StubCritic(), fhir)  # accepts every cited claim

        reply = await _chat(_service(graph_enabled=True, fhir=fhir))

        assert reply.action.value == "served"
        assert reply.passed is True
        assert len(reply.claims) >= 2, f"expected a multi-claim serve, got {reply.claims}"
        assert "aspirin" in _texts(reply)
        assert reply.answer != _WITHHELD_ANSWER


# --- (4) unsafe_action still withholds (unchanged) --------------------------


class TestUnsafeActionStillWithholds:
    async def test_graph_unsafe_action_still_withholds(
        self, _db: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pre-existing ``unsafe_action`` withhold is untouched by option (a)."""
        fhir = _reader()
        critic = _UnsafeCritic(lambda t: "aspirin" in t.lower())
        _inject_critic(monkeypatch, critic, fhir)

        reply = await _chat(_service(graph_enabled=True, fhir=fhir), message=SUMMARY_Q)

        assert reply.action.value == "withheld"
        assert reply.passed is False
        assert reply.claims == []
        assert reply.answer == _WITHHELD_ANSWER
