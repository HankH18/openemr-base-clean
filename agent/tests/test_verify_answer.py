"""Serve-time verification: live re-fetch + deterministic gate, fail-closed.

Exercises ``copilot.verification.serve.verify_answer`` end-to-end with an
in-memory async fake FHIR client (no network).  The gate itself is tested
in ``test_verification_core.py``; here we prove the serve-time *caller*
re-fetches by ID and fails closed when a citation cannot be resolved.
"""

from __future__ import annotations

from typing import Any

import pytest

from copilot.domain.contracts import Claim, VerificationAction
from copilot.domain.primitives import FhirReference, PatientId, ResourceType
from copilot.fhir.client import FhirClientError
from copilot.verification.serve import verify_answer

pytestmark = pytest.mark.asyncio

_PATIENT = PatientId(value=1015)


# --- Fixtures / helpers ----------------------------------------------------


def _obs(value: str = "2.34", *, obs_id: str = "trop-1", abnormal: str = "HH") -> dict[str, Any]:
    """A troponin Observation whose body carries its own resourceType + id."""
    return {
        "resourceType": "Observation",
        "id": obs_id,
        "status": "final",
        "code": {"text": "Troponin I", "coding": [{"code": "6598-7", "display": "Troponin I"}]},
        "valueQuantity": {"value": float(value), "unit": "ng/mL"},
        "interpretation": [{"coding": [{"code": abnormal, "display": "critical high"}]}],
    }


def _claim(
    text: str,
    *,
    obs_id: str = "trop-1",
    field: str = "valueQuantity.value",
    value: str = "2.34",
) -> Claim:
    return Claim(
        text=text,
        source_ref=FhirReference(
            resource_type=ResourceType.Observation,
            resource_id=obs_id,
            field=field,
            value=value,
        ),
    )


class _FakeFhir:
    """In-memory async FHIR reader.

    ``resources`` maps ``(ResourceType, id)`` to a canned body; anything in
    ``errors`` raises on read (simulated fetch failure); a key absent from
    both raises a not-found error.  ``reads`` records every call so tests
    can prove a *live* re-fetch happened.
    """

    def __init__(
        self,
        resources: dict[tuple[ResourceType, str], dict[str, Any]] | None = None,
        *,
        errors: set[tuple[ResourceType, str]] | None = None,
    ) -> None:
        self._resources = dict(resources or {})
        self._errors = set(errors or set())
        self.reads: list[tuple[ResourceType, str]] = []

    async def read(self, resource_type: ResourceType, resource_id: str) -> dict[str, Any]:
        key = (resource_type, resource_id)
        self.reads.append(key)
        if key in self._errors:
            raise FhirClientError(
                f"simulated fetch failure for {resource_type.value}/{resource_id}"
            )
        if key not in self._resources:
            raise FhirClientError(f"not found: {resource_type.value}/{resource_id}")
        return self._resources[key]


# --- (a) cited resources exist and match -> served -------------------------


class TestServedWhenAllCitationsResolve:
    async def test_all_claims_verify_against_live_refetch(self) -> None:
        fake = _FakeFhir(
            {
                (ResourceType.Observation, "trop-1"): _obs("2.34", obs_id="trop-1"),
                (ResourceType.Observation, "bnp-1"): _obs("0.02", obs_id="bnp-1", abnormal=""),
            }
        )
        claims = [
            _claim("Troponin I 2.34 ng/mL — critical high.", obs_id="trop-1", value="2.34"),
            _claim("BNP 0.02.", obs_id="bnp-1", value="0.02"),
        ]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.served
        assert result.passed is True
        assert all(c.attribution_ok and c.value_match for c in result.claims)
        # Proves the resources were fetched fresh by ID.
        assert (ResourceType.Observation, "trop-1") in fake.reads
        assert (ResourceType.Observation, "bnp-1") in fake.reads

    async def test_duplicate_citation_fetched_once(self) -> None:
        fake = _FakeFhir({(ResourceType.Observation, "trop-1"): _obs("2.34")})
        claims = [
            _claim("Troponin 2.34.", value="2.34"),
            _claim("Troponin remains 2.34.", value="2.34"),
        ]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.served
        assert fake.reads.count((ResourceType.Observation, "trop-1")) == 1

    async def test_domain_flag_surfaces_on_served_answer(self) -> None:
        """Critical-lab flag is surfaced even when every claim passes."""
        fake = _FakeFhir({(ResourceType.Observation, "trop-1"): _obs("2.34", abnormal="HH")})
        claims = [_claim("Troponin 2.34.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.served
        assert any(f.rule == "critical_lab" for f in result.domain_flags)


# --- (b) citation fails to fetch -> fail-closed ----------------------------


class TestFailClosedOnUnfetchableCitation:
    async def test_read_raises_withholds_the_only_claim(self) -> None:
        fake = _FakeFhir(errors={(ResourceType.Observation, "trop-1")})
        claims = [_claim("Troponin 2.34.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.withheld
        assert result.passed is False
        assert result.claims[0].attribution_ok is False

    async def test_missing_resource_not_assumed_true(self) -> None:
        # Nothing seeded and no explicit error -> read raises not-found.
        fake = _FakeFhir()
        claims = [_claim("Troponin 2.34.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False

    async def test_empty_body_treated_as_unverifiable(self) -> None:
        """A read that returns nothing (empty dict) must fail closed."""
        fake = _FakeFhir({(ResourceType.Observation, "trop-1"): {}})
        claims = [_claim("Troponin 2.34.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is False

    async def test_mixed_fetchable_and_unfetchable_degrades(self) -> None:
        fake = _FakeFhir(
            {(ResourceType.Observation, "trop-1"): _obs("2.34")},
            errors={(ResourceType.Observation, "bnp-1")},
        )
        good = _claim("Troponin 2.34.", obs_id="trop-1", value="2.34")
        bad = _claim("BNP 0.02.", obs_id="bnp-1", value="0.02")
        result = await verify_answer([good, bad], _PATIENT, fake)

        assert result.action == VerificationAction.degraded
        by_id = {c.source_ref.resource_id: c for c in result.claims}
        assert by_id["trop-1"].value_match is True
        assert by_id["bnp-1"].attribution_ok is False


# --- (c) numeric value not present in the re-fetch -> value-match fails -----


class TestValueMatchAgainstLiveData:
    async def test_drifted_value_fails_match(self) -> None:
        """Answer asserts 2.34 but the record now reads 0.23 -> withheld."""
        fake = _FakeFhir({(ResourceType.Observation, "trop-1"): _obs("0.23")})
        claims = [_claim("Troponin 2.34 ng/mL.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.withheld
        assert result.claims[0].attribution_ok is True
        assert result.claims[0].value_match is False
        assert "mismatch" in result.claims[0].reason

    async def test_extra_numeric_literal_absent_from_source_fails(self) -> None:
        """Claim text smuggles a number the re-fetched resource never had."""
        fake = _FakeFhir({(ResourceType.Observation, "trop-1"): _obs("2.34")})
        claims = [_claim("Troponin 2.34, up from 0.02.", value="2.34")]
        result = await verify_answer(claims, _PATIENT, fake)

        assert result.action == VerificationAction.withheld
        assert result.claims[0].value_match is False
        assert "0.02" in result.claims[0].reason
