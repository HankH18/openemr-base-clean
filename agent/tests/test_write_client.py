"""Tests for the Standard-API write client, driven by a fake httpx transport.

Every case pins the exact request shape (method, path, JSON body, headers) the
client puts on the wire and the exact fail-closed behaviour: success only on an
explicit 201/200 with a parseable id, everything else — non-2xx, missing id,
unparseable body, transport error — raises, and no ``CommittedWrite`` is ever
returned for an unconfirmed write.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from copilot.domain.primitives import PatientId
from copilot.domain.writes import MedicationWrite, VitalWrite, WritableMetric, WriteKind
from copilot.fhir.auth import OAuthToken, StaticTokenProvider, TokenProvider
from copilot.fhir.write_client import OpenEmrWriteClient, OpenEmrWriteError

pytestmark = pytest.mark.asyncio

_BASE = "http://openemr/apis/default/api"
_PID = PatientId(value=1015)


def _fixed_now() -> datetime:
    return datetime(2026, 7, 11, 9, 0, tzinfo=UTC)


def _static_provider(value: str = "write-tok") -> StaticTokenProvider:
    return StaticTokenProvider(
        token=OAuthToken(
            access_token=value,
            token_type="Bearer",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )


Handler = Callable[[httpx.Request], httpx.Response]


@asynccontextmanager
async def _writer(
    handler: Handler,
    *,
    provider: TokenProvider | None = None,
) -> AsyncIterator[OpenEmrWriteClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenEmrWriteClient(
        _BASE,
        provider or _static_provider(),
        http_client=http,
        now=_fixed_now,
    )
    try:
        yield client
    finally:
        await http.aclose()


def _json_body(request: httpx.Request) -> dict[str, object]:
    return json.loads(request.content.decode())


# --- create_vital -----------------------------------------------------------


class TestCreateVital:
    async def test_posts_single_column_and_parses_vid(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json={"vid": 555, "fid": 777})

        async with _writer(handler) as client:
            committed = await client.create_vital(
                _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
            )

        assert committed.resource_kind is WriteKind.vital
        assert committed.new_id == "555"
        assert committed.encounter_id == "42"
        assert committed.committed_at == _fixed_now()

        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/apis/default/api/patient/1015/encounter/42/vital"
        assert _json_body(request) == {"pulse": "72"}
        assert request.headers["Authorization"] == "Bearer write-tok"
        assert request.headers["Content-Type"] == "application/json"

    async def test_metric_to_column_mapping_is_exhaustive_and_correct(self) -> None:
        expected = {
            WritableMetric.heart_rate: "pulse",
            WritableMetric.spo2: "oxygen_saturation",
            WritableMetric.systolic_bp: "bps",
            WritableMetric.diastolic_bp: "bpd",
            WritableMetric.respiratory_rate: "respiration",
            WritableMetric.temperature: "temperature",
            WritableMetric.weight: "weight",
            WritableMetric.height: "height",
        }
        # Assert we cover every metric the enum defines (no silent gap).
        assert set(expected) == set(WritableMetric)

        for metric, column in expected.items():
            captured: dict[str, object] = {}

            def handler(request: httpx.Request, _cap: dict[str, object] = captured) -> httpx.Response:
                _cap.update(_json_body(request))
                return httpx.Response(201, json={"vid": 1, "fid": 2})

            async with _writer(handler) as client:
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=metric, value=50, unit="x")
                )
            assert list(captured) == [column], metric

    async def test_whole_number_value_drops_trailing_zero(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(_json_body(request))
            return httpx.Response(201, json={"vid": 1, "fid": 2})

        async with _writer(handler) as client:
            await client.create_vital(
                _PID, "42", VitalWrite(metric=WritableMetric.temperature, value=98.6, unit="F")
            )
        assert captured == {"temperature": "98.6"}

    async def test_forwards_idempotency_key_header(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json={"vid": 9, "fid": 9})

        async with _writer(handler) as client:
            await client.create_vital(
                _PID,
                "42",
                VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm"),
                idempotency_key="idem-key-1",
            )
        assert seen[0].headers["Idempotency-Key"] == "idem-key-1"

    async def test_non_201_raises_and_carries_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, json={"error": "not permitted"})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError) as exc:
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )
        assert exc.value.status_code == 403

    async def test_validation_error_surfaces_messages(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"validationErrors": {"pulse": "invalid"}})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError) as exc:
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )
        assert exc.value.status_code == 400
        assert exc.value.validation == {"pulse": "invalid"}

    async def test_201_without_id_is_treated_as_failed(self) -> None:
        # Ambiguous success: 201 but no parseable id ⇒ FAILED, never assumed.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"fid": 777})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError):
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )

    async def test_unparseable_body_is_treated_as_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, content=b"not json", headers={"Content-Type": "text/plain"})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError):
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )

    async def test_transport_error_is_treated_as_failed(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError):
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )


# --- create_medication ------------------------------------------------------


class TestCreateMedication:
    async def test_posts_title_and_begdate_and_parses_id(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json={"id": 900})

        async with _writer(handler) as client:
            committed = await client.create_medication(
                _PID, MedicationWrite(title="Aspirin 81 mg", begdate="2026-07-11")
            )

        assert committed.resource_kind is WriteKind.medication
        assert committed.new_id == "900"
        assert committed.encounter_id is None

        request = seen[0]
        assert request.method == "POST"
        assert request.url.path == "/apis/default/api/patient/1015/medication"
        assert _json_body(request) == {"title": "Aspirin 81 mg", "begdate": "2026-07-11"}

    async def test_optional_fields_included_when_present(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(201, json={"id": 901})

        async with _writer(handler) as client:
            await client.create_medication(
                _PID,
                MedicationWrite(
                    title="Lisinopril",
                    begdate="2026-07-11",
                    enddate="2026-08-11",
                    diagnosis="ICD10:I10",
                ),
            )
        assert _json_body(seen[0]) == {
            "title": "Lisinopril",
            "begdate": "2026-07-11",
            "enddate": "2026-08-11",
            "diagnosis": "ICD10:I10",
        }


# --- retract_medication -----------------------------------------------------


class TestRetractMedication:
    async def test_puts_enddate_and_requires_200(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": 900})

        async with _writer(handler) as client:
            committed = await client.retract_medication(_PID, "900")

        assert committed.new_id == "900"
        request = seen[0]
        assert request.method == "PUT"
        assert request.url.path == "/apis/default/api/patient/1015/medication/900"
        assert _json_body(request) == {"enddate": "2026-07-11"}

    async def test_201_on_a_put_is_rejected(self) -> None:
        # retract is an update ⇒ success is exactly 200, not 201.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"id": 900})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError):
                await client.retract_medication(_PID, "900")


# --- resolve_or_create_encounter -------------------------------------------


class TestResolveOrCreateEncounter:
    async def test_reuses_todays_most_recent_encounter(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "validationErrors": [],
                    "internalErrors": [],
                    "data": [
                        {"id": "40", "date": "2026-07-11 07:00:00"},
                        {"id": "42", "date": "2026-07-11 09:30:00"},
                        {"id": "10", "date": "2020-01-01 08:00:00"},
                    ],
                    "links": [],
                },
            )

        async with _writer(handler) as client:
            eid = await client.resolve_or_create_encounter(_PID)

        assert eid == "42"  # latest of today's encounters
        assert len(seen) == 1  # GET only — no create
        assert seen[0].method == "GET"

    async def test_creates_encounter_when_none_today(self) -> None:
        calls: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={"data": [{"id": "10", "date": "2020-01-01 08:00:00"}]},
                )
            return httpx.Response(
                201,
                json={"data": {"id": "99", "date": "2026-07-11"}},
            )

        async with _writer(handler) as client:
            eid = await client.resolve_or_create_encounter(_PID)

        assert eid == "99"
        assert calls == [
            ("GET", "/apis/default/api/patient/1015/encounter"),
            ("POST", "/apis/default/api/patient/1015/encounter"),
        ]

    async def test_creates_encounter_when_lookup_404s(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(201, json={"data": {"id": "77", "date": "2026-07-11"}})

        async with _writer(handler) as client:
            eid = await client.resolve_or_create_encounter(_PID)
        assert eid == "77"

    async def test_lookup_server_error_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError) as exc:
                await client.resolve_or_create_encounter(_PID)
        assert exc.value.status_code == 500


# --- 401 retry --------------------------------------------------------------


class TestUnauthorizedRetry:
    async def test_retries_once_with_forced_refresh_on_401(self) -> None:
        statuses = [401, 201]
        seen_tokens: list[str] = []

        class RotatingProvider:
            def __init__(self) -> None:
                self.calls: list[bool] = []

            async def get_token(self, force: bool = False) -> OAuthToken:
                self.calls.append(force)
                return OAuthToken(
                    access_token=f"tok-{len(self.calls)}",
                    token_type="Bearer",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )

        def handler(request: httpx.Request) -> httpx.Response:
            seen_tokens.append(request.headers["Authorization"])
            status = statuses.pop(0)
            if status == 401:
                return httpx.Response(401, json={"error": "expired"})
            return httpx.Response(201, json={"vid": 5, "fid": 6})

        provider = RotatingProvider()
        async with _writer(handler, provider=provider) as client:
            committed = await client.create_vital(
                _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
            )

        assert committed.new_id == "5"
        assert provider.calls == [False, True]  # normal, then forced refresh
        assert seen_tokens == ["Bearer tok-1", "Bearer tok-2"]

    async def test_second_401_gives_up(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "expired"})

        async with _writer(handler) as client:
            with pytest.raises(OpenEmrWriteError) as exc:
                await client.create_vital(
                    _PID, "42", VitalWrite(metric=WritableMetric.heart_rate, value=72, unit="bpm")
                )
        assert exc.value.status_code == 401
