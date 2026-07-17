"""The graph — not its caller — owns the critic's unsafe withhold.

Before this, ``ChatService`` held the only ``critic.unsafe`` check, so the graph
itself returned ``action=served, passed=True`` **and the full prose** on a turn its
own critic had condemned. Two live consequences (graph mode is on in the deployed
demo), both observed:

1. **Metric corruption, present-day.** ``AgentGraph.run`` calls
   ``record_verification`` with the un-withheld verification, and graph mode
   deliberately records no second event. So every unsafe withhold was logged to the
   safety dashboard as ``served``: the clinician was correctly protected while the
   one metric that proves the safety pass fires reported the exact opposite.
   Telemetry that lies in the safest-looking direction.
2. **Latent bypass.** ``build_graph`` is exported in ``graph/__init__.__all__``. Any
   caller reading ``result.verification.action`` — eval harness, batch job, CLI —
   serves prose the critic condemned.

These assert against the GRAPH's own return value and the recorded metric, never
through ChatService: routing through the collaborator is the blind spot that let
this survive in the first place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from copilot.config import Settings
from copilot.domain.contracts import VerificationAction
from copilot.graph.contracts import AgentTask, CriticVerdict
from copilot.graph.critic import ReviewableClaim
from copilot.graph.factory import build_graph
from copilot.observability.base import NoopObservability


class _RecordingObs(NoopObservability):
    """Captures exactly what the safety dashboard would have been told."""

    def __init__(self) -> None:
        self.verifications: list[tuple[str, bool]] = []

    def record_verification(self, *, passed: bool, action: str, patient_id: int) -> None:
        self.verifications.append((action, passed))


_CONDEMNED = "Give 10x the insulin dose."


class _UnsafeCritic:
    """The keyed critic's unsafe_action path.

    Condemns unconditionally rather than mapping over the claims it is handed: on
    the keyless path the stub agent grounds no FHIR claims, so a critic that only
    condemns what it is given returns an EMPTY unsafe set and the test passes
    while proving nothing. That vacuous pass is the exact shape this file exists
    to catch, so the fixture must be able to condemn a turn with zero claims —
    which is also the real danger (the critic condemns the PROSE, not a citation).
    """

    def __init__(self, unsafe: bool = True) -> None:
        self._unsafe = unsafe

    def review(self, claims: Sequence[ReviewableClaim]) -> CriticVerdict:
        if not self._unsafe:
            return CriticVerdict(accepted=[], rejected=[])
        return CriticVerdict(accepted=[], rejected=[_CONDEMNED], unsafe=[_CONDEMNED])


def _graph(obs: _RecordingObs, *, unsafe: bool) -> Any:
    graph = build_graph(Settings(), observability=obs)
    graph._critic = _UnsafeCritic(unsafe=unsafe)  # inject past the keyless factory
    return graph


_TASK = AgentTask(patient_id=1, question="What is the troponin trend?")


@pytest.mark.anyio
async def test_graph_withholds_prose_its_own_critic_called_unsafe() -> None:
    obs = _RecordingObs()
    result = await _graph(obs, unsafe=True).run(_TASK)

    assert result.verification.action is VerificationAction.withheld, (
        "the graph must not hand back a `served` verdict on a turn its own critic "
        "called unsafe — an exported-build_graph caller would serve the prose"
    )
    assert result.verification.passed is False


@pytest.mark.anyio
async def test_the_safety_metric_records_the_withhold_not_a_serve() -> None:
    obs = _RecordingObs()
    await _graph(obs, unsafe=True).run(_TASK)

    assert obs.verifications, "the graph must record a verification event"
    action, passed = obs.verifications[-1]
    assert action == "withheld", (
        f"the safety dashboard was told {action!r} for an unsafe withhold. This is "
        "the metric that proves the safety pass fires; it reported its opposite."
    )
    assert passed is False


@pytest.mark.anyio
async def test_a_safe_turn_is_still_served() -> None:
    # The guard must bite ONLY on unsafe. Without this, withholding unconditionally
    # would pass both tests above for entirely the wrong reason — a vacuous pass.
    obs = _RecordingObs()
    result = await _graph(obs, unsafe=False).run(_TASK)

    assert result.verification.action is not VerificationAction.withheld
    assert obs.verifications[-1][0] != "withheld"
