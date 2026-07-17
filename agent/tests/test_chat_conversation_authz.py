"""Cross-patient authorization on a caller-supplied ``conversation_id`` (POST /v1/chat).

The sibling of the already-closed ``GET /v1/conversations/{id}`` leak: ``POST
/v1/chat`` accepts a ``conversation_id`` in the body. Before the fix guarded here,
that id was echoed verbatim — no ownership check — so any clinician authorized for
even ONE patient could supply another patient's autoincrement conversation id and
have that foreign thread's PHI replayed into the answer's LLM context, the new
turns appended into it, and the foreign id returned. The read-audit row was written
under the request's ``patient_id``, misattributing the access.

The fix (``ChatService._resolve_conversation``) refuses a supplied id unless it
belongs to the request's patient — the SAME patient-level (rounding-list) boundary
the GET route enforces, not conversation ownership. A foreign OR nonexistent id
raises ``ConversationAccessError``, which the route maps to the same
indistinguishable 404 (same detail) the GET route uses, so existence is never an
oracle.

These tests drive the REAL service/route path against a temp-file SQLite DB (no
mocks of the code under test). The FHIR reader is replaced by an in-memory double
only for the one *served* regression test; every refusal test raises before any
FHIR client is built.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from copilot.chat.service import ChatService, ConversationAccessError
from copilot.domain.primitives import ClinicianId, PatientId, ResourceType

# Two clinicians, two patients. A is authorized (rounding list) for P only; B owns a
# thread about Q and is a stranger to A. The attack: A supplies B's conversation id.
CLIN_A = 8001
CLIN_B = 8002
P = 1001  # A's patient — also the FHIR-cohort patient, so the served path grounds.
Q = 999  # B's patient — the foreign thread's PHI subject.

# The detail string the GET route uses for BOTH "not found" and "not yours"; the
# POST refusal must be byte-identical. Imported from the route module so a rename
# there fails this test rather than silently drifting.
from copilot.api.routes.chat import _CONVERSATION_NOT_FOUND_DETAIL  # noqa: E402

_PHI = [
    ("user", "Does this patient have HIV?"),
    ("assistant", "Yes — HIV+, viral load 40,000 copies/mL."),
]


# --- FHIR double (only the served regression test reaches it) --------------


def _obs(rid: str, text: str, value: float, unit: str, interp: str | None) -> dict[str, Any]:
    res: dict[str, Any] = {
        "resourceType": "Observation",
        "id": rid,
        "meta": {"lastUpdated": "2026-07-09T06:30:00Z"},
        "status": "final",
        "code": {"text": text},
        "valueQuantity": {"value": value, "unit": unit},
    }
    if interp is not None:
        res["interpretation"] = [{"coding": [{"code": interp}]}]
    return res


_COHORT: dict[str, dict[str, list[dict[str, Any]]]] = {
    "1001": {"Observation": [_obs("obs-1001-trop", "Troponin I", 0.9, "ng/mL", "HH")]},
}


class _FakeFhir:
    """Async-context FHIR double: ``search`` over a cohort, ``read`` by id."""

    def __init__(self, cohort: dict[str, dict[str, list[dict[str, Any]]]]) -> None:
        self._cohort = cohort
        self._by_id: dict[tuple[str, str], dict[str, Any]] = {}
        for bytype in cohort.values():
            for rtype, rlist in bytype.items():
                for r in rlist:
                    self._by_id[(rtype, r["id"])] = r

    async def __aenter__(self) -> _FakeFhir:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def search(self, rtype: ResourceType, params: dict[str, str]) -> dict[str, Any]:
        resources = self._cohort.get(params.get("patient", ""), {}).get(rtype.value, [])
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r} for r in resources],
        }

    async def read(self, rtype: ResourceType, rid: str) -> dict[str, Any]:
        res = self._by_id.get((rtype.value, rid))
        if res is None:
            raise RuntimeError(f"no such resource {rtype.value}/{rid}")
        return res


# --- fixtures + helpers ----------------------------------------------------


@pytest.fixture
def _db_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point Settings at a temp SQLite file and create the schema."""
    from copilot.config import get_settings
    from copilot.memory.db import Base, get_engine, get_session_factory

    db_file = tmp_path / "chat_authz.db"
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    monkeypatch.setenv("COPILOT_FHIR_BASE_URL", "http://oe.test/fhir")
    monkeypatch.setenv("COPILOT_ANTHROPIC_API_KEY", "")  # -> deterministic stub agent
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


@pytest.fixture(autouse=True)
def _fake_fhir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the service's FHIR reader with the in-memory cohort double.

    Only the served regression test reaches it; the refusal tests raise before a
    FHIR client is ever built. A test that needs the guard to *fail loudly* rather
    than serve overrides this in its own body.
    """
    monkeypatch.setattr(ChatService, "_fhir_client", lambda self: _FakeFhir(_COHORT))


def _run[T](make_coro: Callable[[], Coroutine[Any, Any, T]]) -> T:
    """Run one async body on a fresh event loop with fresh engine caches.

    The async engine is bound to the loop that created it; each ``asyncio.run``
    opens (and closes) its own loop, so the cache must be cleared first or a later
    body would reuse an engine bound to a dead loop. State survives across runs
    because the DB is a temp *file*.
    """
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return asyncio.run(make_coro())


async def _seed_conversation(clinician: int, patient: int, messages: list[tuple[str, str]]) -> int:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        repo = MemoryRepository(session)
        cid = await repo.create_conversation(
            ClinicianId(value=clinician), PatientId(value=patient), "seed-corr-0001"
        )
        for role, content in messages:
            await repo.append_message(cid, role, content)
        return cid


async def _authorize(clinician: int, patients: list[int]) -> None:
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async with session_scope() as session:
        await MemoryRepository(session).upsert_rounding_cursor(
            ClinicianId(value=clinician), patients, 0, []
        )


def _client() -> TestClient:
    from copilot.api.app import create_app
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _post_chat(
    client: TestClient, *, patient_id: int, conversation_id: int | None = None
) -> Any:
    body: dict[str, Any] = {
        "clinician_id": CLIN_A,
        "patient_id": patient_id,
        "message": "What is the latest troponin value?",
    }
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return client.post("/v1/chat", json=body)


# --- service-level: _resolve_conversation (the authorization choke point) ---


def test_foreign_conversation_id_is_refused(_db_file: str) -> None:
    """A supplied id owned by another patient is refused (the core of the leak)."""
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async def _body() -> None:
        conv_b = await _seed_conversation(CLIN_B, Q, _PHI)
        service = ChatService(get_settings())
        async with session_scope() as session:
            repo = MemoryRepository(session)
            with pytest.raises(ConversationAccessError):
                await service._resolve_conversation(
                    repo,
                    ClinicianId(value=CLIN_A),
                    PatientId(value=P),
                    "attacker-corr-0001",
                    conv_b,
                )

    _run(_body)


def test_nonexistent_conversation_id_is_refused(_db_file: str) -> None:
    """A supplied id that does not exist is refused the same way as a foreign one."""
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async def _body() -> None:
        service = ChatService(get_settings())
        async with session_scope() as session:
            repo = MemoryRepository(session)
            with pytest.raises(ConversationAccessError):
                await service._resolve_conversation(
                    repo,
                    ClinicianId(value=CLIN_A),
                    PatientId(value=P),
                    "attacker-corr-0002",
                    987654,
                )

    _run(_body)


def test_owner_continuing_own_thread_is_allowed(_db_file: str) -> None:
    """The regression guard: continuing your OWN patient's thread still resolves."""
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async def _body() -> int:
        conv_a = await _seed_conversation(CLIN_A, P, [("user", "hi")])
        service = ChatService(get_settings())
        async with session_scope() as session:
            repo = MemoryRepository(session)
            resolved = await service._resolve_conversation(
                repo,
                ClinicianId(value=CLIN_A),
                PatientId(value=P),
                "owner-corr-0001",
                conv_a,
            )
        assert resolved == conv_a
        return conv_a

    _run(_body)


def test_fresh_thread_opens_new_conversation(_db_file: str) -> None:
    """No conversation_id (a fresh thread) still opens a new patient-scoped one."""
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    async def _body() -> None:
        service = ChatService(get_settings())
        async with session_scope() as session:
            repo = MemoryRepository(session)
            new_id = await service._resolve_conversation(
                repo,
                ClinicianId(value=CLIN_A),
                PatientId(value=P),
                "fresh-corr-0001",
                None,
            )
        # A real row was created, owned by the acting clinician + request patient.
        async with session_scope() as session:
            row = await MemoryRepository(session).get_conversation(new_id)
        assert row is not None
        assert row.patient_id == P
        assert row.clinician_id == CLIN_A

    _run(_body)


# --- service-level: full chat() — PHI not loaded, nothing appended ----------


def test_foreign_thread_phi_is_not_loaded_and_not_appended(
    _db_file: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headline: A's turn on B's conversation_id loads NONE of B's PHI and writes nothing.

    Drives the real ``ChatService.chat`` path. Proves three things at once: the
    call is refused, ``get_conversation_messages`` is never invoked with the
    foreign id inside the answer path (so B's PHI never reaches the LLM context),
    and B's thread is byte-for-byte unchanged afterward (no turn appended).
    """
    from copilot.config import get_settings
    from copilot.memory.db import session_scope
    from copilot.memory.repository import MemoryRepository

    loaded_ids: list[int] = []
    orig_get_messages = MemoryRepository.get_conversation_messages

    async def _spy_get_messages(self: MemoryRepository, conversation_id: int) -> Any:
        loaded_ids.append(conversation_id)
        return await orig_get_messages(self, conversation_id)

    monkeypatch.setattr(MemoryRepository, "get_conversation_messages", _spy_get_messages)

    # If the guard regressed, the turn would proceed to build a FHIR reader — fail
    # loudly here instead of letting a broken guard reach the network / serve.
    def _no_fhir(self: ChatService) -> Any:
        raise AssertionError("guard regressed: FHIR reader built for a refused foreign conversation")

    monkeypatch.setattr(ChatService, "_fhir_client", _no_fhir)

    state: dict[str, Any] = {}

    async def _body() -> None:
        conv_b = await _seed_conversation(CLIN_B, Q, _PHI)
        service = ChatService(get_settings())
        raised = False
        try:
            await service.chat(
                clinician_id=ClinicianId(value=CLIN_A),
                patient_id=PatientId(value=P),
                message="Summarize this thread for me.",
                correlation_id="attacker-corr-0003",
                conversation_id=conv_b,
            )
        except ConversationAccessError:
            raised = True
        # Snapshot the loads that happened DURING the refused chat, before we
        # re-read B's thread ourselves below (which would otherwise add conv_b).
        loads_during_chat = list(loaded_ids)
        async with session_scope() as session:
            after = await MemoryRepository(session).get_conversation_messages(conv_b)
        state["conv_b"] = conv_b
        state["raised"] = raised
        state["loads_during_chat"] = loads_during_chat
        state["after"] = [(m.role, m.content) for m in after]

    _run(_body)

    assert state["raised"], "supplying another patient's conversation_id must be refused"
    assert state["conv_b"] not in state["loads_during_chat"], (
        "the foreign thread's history must never be loaded into the answer path"
    )
    assert state["after"] == _PHI, (
        "no turn may be appended into a foreign patient's thread; it must be unchanged"
    )


# --- route-level: refusal shape matches GET, indistinguishable --------------


def test_foreign_and_nonexistent_conversation_ids_are_indistinguishable(_db_file: str) -> None:
    """Over HTTP: a foreign id and a nonexistent id yield the SAME 404 + SAME body.

    And that body matches the GET route's own refusal for the same foreign id, so
    the two PHI surfaces are consistent: existence is an oracle on neither.
    """
    conv_b = _run(lambda: _seed_conversation(CLIN_B, Q, _PHI))
    _run(lambda: _authorize(CLIN_A, [P]))

    client = _client()
    foreign = _post_chat(client, patient_id=P, conversation_id=conv_b)
    nonexistent = _post_chat(client, patient_id=P, conversation_id=987654)

    assert foreign.status_code == 404, f"a foreign conversation_id must 404, got {foreign.status_code}"
    assert nonexistent.status_code == 404
    assert foreign.json() == {"detail": _CONVERSATION_NOT_FOUND_DETAIL}
    # Byte-identical: 'not yours' cannot be told from 'does not exist'.
    assert (foreign.status_code, foreign.json()) == (nonexistent.status_code, nonexistent.json())
    # No PHI / answer leaks through the refusal body.
    for body in (foreign.json(), nonexistent.json()):
        assert "answer" not in body
        assert "claims" not in body
        assert "conversation_id" not in body

    # The POST refusal matches the sibling GET route's refusal for the same id.
    get_refusal = client.get(f"/v1/conversations/{conv_b}", params={"clinician_id": CLIN_A})
    assert (get_refusal.status_code, get_refusal.json()) == (foreign.status_code, foreign.json()), (
        "POST and GET must refuse a foreign conversation identically"
    )


# --- route-level: owner still works end-to-end (regression) -----------------


def test_owner_continues_thread_over_http(_db_file: str) -> None:
    """The legitimate owner opening then continuing their OWN thread still serves 200."""
    _run(lambda: _authorize(CLIN_A, [P]))
    client = _client()

    first = _post_chat(client, patient_id=P)
    assert first.status_code == 200, f"owner's fresh turn must serve, got {first.status_code}"
    conv_id = first.json()["conversation_id"]
    assert isinstance(conv_id, int)

    second = _post_chat(client, patient_id=P, conversation_id=conv_id)
    assert second.status_code == 200, (
        f"owner continuing their own thread must still serve, got {second.status_code}"
    )
    assert second.json()["conversation_id"] == conv_id
