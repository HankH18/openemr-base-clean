"""P3 bite-proof: StubSynthesizer claims must ground their source timestamp.

The chat/summary paths (``agent/stub.py``, ``agent/claude.py``,
``rounds/summary.py``) all build each ``FhirReference`` with
``timestamp=extract_temporal(res)`` so the verifier's temporal gate has a
grounded time to re-compare on live re-fetch. ``StubSynthesizer.synthesize``
(``worker/synthesizer.py``) built its ``FhirReference`` with ``field`` / ``value``
/ ``unit`` but NO ``timestamp=`` — so a memory-file claim persisted by the
poller/refresh path carried ``timestamp=None`` and the temporal gate was inert
for it. This is the parity fix.

Bite: before the fix ``source_ref.timestamp`` is ``None`` for an Observation
that carries an ``effectiveDateTime``; after the fix it is the grounded time.
Reverting the ``timestamp=`` argument flips it back to ``None`` and reddens this.
No pre-existing test guards the stub's temporal grounding.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot.agent.grounding import extract_temporal
from copilot.domain.primitives import PatientId
from copilot.worker.synthesizer import StubSynthesizer, SynthesisInput

pytestmark = pytest.mark.asyncio


async def test_stub_synthesizer_grounds_source_timestamp() -> None:
    resource = {
        "resourceType": "Observation",
        "id": "trop-1",
        "status": "final",
        "effectiveDateTime": "2026-07-08T12:00:00Z",
        "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
    }
    synth = StubSynthesizer()
    inputs = SynthesisInput(
        patient_id=PatientId(value=1015),
        resources=[resource],
        source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
    )

    summary = await synth.synthesize(inputs)

    assert len(summary.claims) == 1
    ref = summary.claims[0].source_ref
    # The grounding helper reads effectiveDateTime; the stub must carry it so the
    # verifier's temporal gate is live for poller/refresh-persisted claims.
    assert extract_temporal(resource) == "2026-07-08T12:00:00Z"
    assert ref.timestamp is not None, (
        "StubSynthesizer must ground source_ref.timestamp (temporal-gate parity "
        "with agent/stub.py, agent/claude.py, rounds/summary.py) — None here means "
        "the temporal gate is inert for memory-file claims"
    )
    assert ref.timestamp == datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


async def test_stub_synthesizer_timestamp_none_when_source_has_no_time() -> None:
    # A resource with no effectiveDateTime/issued grounds no timestamp — the gate
    # short-circuits (None means "nothing to re-verify"), matching extract_temporal.
    resource = {
        "resourceType": "Observation",
        "id": "no-time",
        "status": "final",
        "valueQuantity": {"value": 5.7, "unit": "mmol/L"},
    }
    synth = StubSynthesizer()
    inputs = SynthesisInput(
        patient_id=PatientId(value=1),
        resources=[resource],
        source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
    )

    summary = await synth.synthesize(inputs)

    assert len(summary.claims) == 1
    assert extract_temporal(resource) is None
    assert summary.claims[0].source_ref.timestamp is None
