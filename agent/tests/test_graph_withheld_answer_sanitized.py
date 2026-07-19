"""P3: a pure verifier-``withheld`` turn must scrub the LLM prose from the result.

``AgentGraph.run`` blanks ``GraphResult.answer`` to the honest insufficient
message on ``capped``, and on the three fail-open cases ``_should_withhold``
escalates (``degraded`` / critic ``unsafe`` / narrative inconsistency). It did
NOT cover the case where the *deterministic verifier itself* returns
``action == withheld`` — every claim failed the live re-fetch. In that path
``_should_withhold`` returns ``False`` (not ``degraded``; there are no PASSED
claims to intersect the critic's ``rejected`` set), so on the pre-fix code the
verdict is ``withheld`` while ``GraphResult.answer`` still carries the agent's
ungrounded prose.

``ChatService`` is safe (``_answer_via_graph`` re-withholds because nothing
survives both gates), so this only bites an external ``build_graph`` consumer
that renders ``result.answer`` while reading ``result.verification.action`` —
the exact exported-caller rationale the ``capped``/``_should_withhold`` mirroring
was written for. These tests assert against the GRAPH's own return value, never
through ChatService, so the blind spot that let this survive is not re-created.

The pure-withheld verification is injected by patching the verifier the graph
calls (``copilot.graph.supervisor.verify_answer``) to return an all-claims-failed
``withheld`` result, while the real ``StubAgent`` still produces grounded prose
and the real ``StubCritic`` still runs — so only the verify step is doubled and
the rest of ``run()`` is exercised for real.

RED before the fix: ``result.answer`` was the stub's "Based on this patient's
record: ..." prose. GREEN after: it is ``_INSUFFICIENT_ANSWER``.
"""

from __future__ import annotations

import pytest

from copilot.domain.contracts import (
    VerificationAction,
    VerificationClaimResult,
    VerificationResult,
)
from copilot.domain.primitives import FhirReference, ResourceType
from copilot.graph.contracts import AgentTask
from copilot.graph.factory import build_graph
from copilot.graph.supervisor import _INSUFFICIENT_ANSWER, AgentGraph
from copilot.observability import NoopObservability

# Reuse the in-memory FHIR double + synthetic cohort the graph tests already pin.
from tests.test_chat_routes import _COHORT, SICK, _FakeFhir
from tests.test_graph import _keyless

# A plain chart question: "troponin" token-matches SICK's Observation display, so
# the StubAgent grounds a real claim and emits "Based on this patient's record:
# ..." prose — but it routes to NEITHER worker (no guideline intent), so the run
# reaches _finalize on the chart-only path.
_CHART_Q = "What is the latest troponin value?"


def _withheld_all_failed() -> VerificationResult:
    """A deterministic verifier verdict where every cited claim failed the re-fetch.

    Shape matches ``core._to_result``'s ``passed_count == 0`` branch: ``action``
    is ``withheld`` with a NON-empty ``claims`` list whose sole member did not
    pass (``value_match=False``). That is precisely the state in which
    ``_should_withhold`` returns ``False`` — the defect's trigger.
    """
    failed = VerificationClaimResult(
        text="troponin drifted on re-fetch",
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id="obs-1001-trop",
            field="valueQuantity.value",
            value="1.5",
        ),
        attribution_ok=True,
        value_match=False,
        reason="value mismatch at valueQuantity.value: source=1.5 claim=0.9",
    )
    return VerificationResult(
        passed=False, claims=[failed], action=VerificationAction.withheld
    )


def _graph() -> AgentGraph:
    """The full deterministic stub graph over the in-memory cohort (keyless)."""
    return build_graph(
        _keyless(),
        observability=NoopObservability(),
        fhir_client_factory=lambda: _FakeFhir(_COHORT),
    )


class TestPureVerifierWithheldSanitizesAnswer:
    async def test_withheld_verification_blanks_the_llm_prose(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """action==withheld ⇒ GraphResult.answer is the insufficient message, not prose.

        RED on the pre-fix code: ``_should_withhold`` returns False for a pure
        ``withheld`` verdict, so ``answer`` stayed the stub's grounded prose while
        ``verification.action`` was ``withheld``.
        """

        async def _fake_verify(
            claims: object, patient_id: object, fhir_client: object, entailment: object = None
        ) -> VerificationResult:
            return _withheld_all_failed()

        monkeypatch.setattr("copilot.graph.supervisor.verify_answer", _fake_verify)

        result = await _graph().run(AgentTask(patient_id=SICK, question=_CHART_Q))

        # The verdict is withheld ...
        assert result.verification.action == VerificationAction.withheld
        assert result.verification.passed is False
        # ... so the prose the physician (or an exported-build_graph caller) reads
        # must be the honest insufficient message, never the ungrounded LLM prose.
        assert result.answer == _INSUFFICIENT_ANSWER
        assert "Based on this patient's record" not in result.answer

    async def test_served_turn_still_returns_its_answer(self) -> None:
        """Regression guard: with the real verifier, a grounded turn still serves.

        No verify patch — the StubAgent grounds troponin against the cohort and the
        real re-fetch matches, so the turn serves its own prose. The fix must NOT
        over-withhold a served turn.
        """
        result = await _graph().run(AgentTask(patient_id=SICK, question=_CHART_Q))

        assert result.verification.action == VerificationAction.served
        assert result.verification.passed is True
        assert result.answer != _INSUFFICIENT_ANSWER
        assert "Based on this patient's record" in result.answer
