"""Identity parsing + fhirUser→ClinicianId auto-provision (``copilot.auth.identity``)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from copilot.auth.identity import IdentityError, parse_identity, resolve_clinician
from copilot.memory import Base, MemoryRepository

_NOW = datetime(2026, 7, 11, 9, 0, 0, tzinfo=UTC)


def _id_token(**claims: object) -> str:
    """Build an unsigned JWT-shaped token carrying the given payload claims."""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"e30.{payload}.sig"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


class TestParseIdentity:
    def test_extracts_fhir_user_and_profile(self) -> None:
        identity = parse_identity(
            id_token=_id_token(
                fhirUser="https://fhir/Practitioner/uuid-1",
                sub="user-1",
                name="Dr. Alice Attending",
                preferred_username="aattending",
            )
        )
        assert identity.fhir_user == "https://fhir/Practitioner/uuid-1"
        assert identity.subject == "user-1"
        assert identity.display_name == "Dr. Alice Attending"
        assert identity.username == "aattending"

    def test_userinfo_overrides_id_token(self) -> None:
        identity = parse_identity(
            id_token=_id_token(fhirUser="https://fhir/Practitioner/from-token", sub="s"),
            userinfo={"fhirUser": "https://fhir/Practitioner/from-userinfo"},
        )
        assert identity.fhir_user == "https://fhir/Practitioner/from-userinfo"

    def test_missing_fhir_user_raises(self) -> None:
        with pytest.raises(IdentityError):
            parse_identity(id_token=_id_token(sub="user-1", name="No FhirUser"))

    def test_malformed_jwt_raises(self) -> None:
        with pytest.raises(IdentityError):
            parse_identity(id_token="not-a-jwt")


class TestResolveClinician:
    async def test_first_login_provisions_a_row(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        identity = parse_identity(
            id_token=_id_token(fhirUser="https://fhir/Practitioner/uuid-new", name="Dr. New")
        )
        cid = await resolve_clinician(repo, identity, now=_NOW)
        assert cid.value > 0

        row = await repo.get_clinician_by_fhir_user("https://fhir/Practitioner/uuid-new")
        assert row is not None
        assert row.id == cid.value
        assert row.display_name == "Dr. New"
        assert row.last_login_at is not None

    async def test_second_login_reuses_the_same_id(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        identity = parse_identity(id_token=_id_token(fhirUser="https://fhir/Practitioner/stable"))
        first = await resolve_clinician(repo, identity, now=_NOW)
        second = await resolve_clinician(
            repo, identity, now=datetime(2026, 7, 12, 9, 0, 0, tzinfo=UTC)
        )
        assert first.value == second.value

    async def test_distinct_physicians_get_distinct_ids(self, session: AsyncSession) -> None:
        repo = MemoryRepository(session)
        a = await resolve_clinician(
            repo, parse_identity(id_token=_id_token(fhirUser="p/a")), now=_NOW
        )
        b = await resolve_clinician(
            repo, parse_identity(id_token=_id_token(fhirUser="p/b")), now=_NOW
        )
        assert a.value != b.value
