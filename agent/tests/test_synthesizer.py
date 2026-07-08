"""StubSynthesizer + ClaudeSynthesizer output-parsing tests.

Real Anthropic calls are guarded by ``ANTHROPIC_API_KEY`` in the eval
suite; here we mock the client so the parsing logic is exercised
without a live API.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from copilot.domain.primitives import PatientId, ResourceType
from copilot.worker.synthesizer import (
    ClaudeSynthesizer,
    StubSynthesizer,
    SynthesisError,
    SynthesisInput,
)


pytestmark = pytest.mark.asyncio


class TestStubSynthesizer:
    async def test_emits_one_claim_per_input_resource(self) -> None:
        synth = StubSynthesizer()
        inputs = SynthesisInput(
            patient_id=PatientId(value=1015),
            resources=[
                {
                    "resourceType": "Observation",
                    "id": "trop-1",
                    "status": "final",
                    "valueQuantity": {"value": 2.34, "unit": "ng/mL"},
                }
            ],
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
        )
        summary = await synth.synthesize(inputs)
        assert len(summary.claims) == 1
        c = summary.claims[0]
        assert c.source_ref.resource_type == ResourceType.Observation
        assert c.source_ref.resource_id == "trop-1"
        assert c.source_ref.field == "valueQuantity.value"
        assert c.source_ref.value == "2.34"

    async def test_skips_resources_missing_id_or_type(self) -> None:
        synth = StubSynthesizer()
        inputs = SynthesisInput(
            patient_id=PatientId(value=1),
            resources=[{"id": "x"}, {"resourceType": "Observation"}, {}],
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
        )
        summary = await synth.synthesize(inputs)
        assert summary.claims == []

    async def test_content_hash_computed_from_input_resources(self) -> None:
        synth = StubSynthesizer()
        inputs = SynthesisInput(
            patient_id=PatientId(value=1),
            resources=[{"resourceType": "Observation", "id": "1"}],
            source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
        )
        summary = await synth.synthesize(inputs)
        assert len(summary.content_hash) == 64  # sha256 hex


class TestClaudeSynthesizerParsing:
    async def test_parses_valid_json_response(self) -> None:
        fake_response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "Troponin I 2.34 ng/mL (critical high).",
                                    "resource_type": "Observation",
                                    "resource_id": "90045",
                                    "field": "valueQuantity.value",
                                    "value": "2.34",
                                }
                            ],
                            "acuity_score": 8.5,
                            "rank_reason": "Critical trop rise",
                        }
                    )
                )
            ]
        )

        class FakeMessages:
            async def create(self, **_kwargs) -> object:  # noqa: ANN003
                return fake_response

        class FakeClient:
            def __init__(self) -> None:
                self.messages = FakeMessages()

        synth = ClaudeSynthesizer(
            anthropic_api_key="sk-testing", model="claude-sonnet-4-6", client=FakeClient()
        )
        summary = await synth.synthesize(
            SynthesisInput(
                patient_id=PatientId(value=1015),
                resources=[{"resourceType": "Observation", "id": "90045"}],
                source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
            )
        )
        assert len(summary.claims) == 1
        assert summary.acuity_score == 8.5
        assert summary.claims[0].source_ref.resource_type == ResourceType.Observation
        assert summary.claims[0].source_ref.value == "2.34"

    async def test_refuses_to_construct_without_api_key(self) -> None:
        with pytest.raises(SynthesisError):
            ClaudeSynthesizer(anthropic_api_key="", model="claude-sonnet-4-6")

    async def test_raises_on_non_json_output(self) -> None:
        class FakeMessages:
            async def create(self, **_kwargs) -> object:  # noqa: ANN003
                return SimpleNamespace(content=[SimpleNamespace(text="Sorry, I cannot.")])

        class FakeClient:
            def __init__(self) -> None:
                self.messages = FakeMessages()

        synth = ClaudeSynthesizer(
            anthropic_api_key="sk-testing", model="claude-sonnet-4-6", client=FakeClient()
        )
        with pytest.raises(SynthesisError):
            await synth.synthesize(
                SynthesisInput(
                    patient_id=PatientId(value=1),
                    resources=[{"resourceType": "Observation", "id": "1"}],
                    source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
                )
            )

    async def test_raises_on_unknown_resource_type(self) -> None:
        fake_response = SimpleNamespace(
            content=[
                SimpleNamespace(
                    text=json.dumps(
                        {
                            "claims": [
                                {
                                    "text": "unknown",
                                    "resource_type": "NotAResource",
                                    "resource_id": "1",
                                    "field": "x",
                                    "value": "y",
                                }
                            ],
                            "acuity_score": 0.0,
                            "rank_reason": "",
                        }
                    )
                )
            ]
        )

        class FakeMessages:
            async def create(self, **_kwargs) -> object:  # noqa: ANN003
                return fake_response

        class FakeClient:
            def __init__(self) -> None:
                self.messages = FakeMessages()

        synth = ClaudeSynthesizer(
            anthropic_api_key="sk-testing", model="claude-sonnet-4-6", client=FakeClient()
        )
        with pytest.raises(SynthesisError):
            await synth.synthesize(
                SynthesisInput(
                    patient_id=PatientId(value=1),
                    resources=[{"resourceType": "Observation", "id": "1"}],
                    source_watermark=datetime(2026, 7, 8, tzinfo=UTC),
                )
            )
