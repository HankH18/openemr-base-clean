"""Dependency probes used by `/ready`.

Each probe is small, isolated, and returns a `ReadinessDependency`.  The
readiness endpoint composes them.  Kept out of `app.py` so unit tests can
inject fakes without spinning up the full FastAPI app.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from copilot.auth.service import AuthConfigError, ensure_authorize_url_browser_reachable
from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency
from copilot.memory.models import GuidelineChunkRow


class DependencyProbe(Protocol):
    """A callable that returns a ReadinessDependency, async."""

    async def __call__(self) -> ReadinessDependency: ...


def _project_root() -> Path:
    """Repo/image root — the dir holding ``alembic.ini`` and ``migrations/``.

    Resolved from this file (``<root>/copilot/api/readiness.py``) rather than the
    process CWD: uvicorn's working directory is not guaranteed, and a CWD-relative
    ``Config("alembic.ini")`` would silently fail to find the scripts and report a
    false "cannot read migrations" instead of the real schema state.
    """
    return Path(__file__).resolve().parents[2]


def script_directory() -> ScriptDirectory:
    """The alembic ScriptDirectory for this deployment's migration scripts."""
    cfg = Config(str(_project_root() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_project_root() / "migrations"))
    return ScriptDirectory.from_config(cfg)


# SQLAlchemy/DBAPI exception class names that mean "the queried relation does not
# exist" — i.e. the schema was never migrated — as opposed to "the database is
# unreachable". Postgres surfaces a missing table as ProgrammingError wrapping
# psycopg's UndefinedTable; SQLAlchemy reflection raises NoSuchTableError.
_MISSING_RELATION_ERROR_NAMES = frozenset({"ProgrammingError", "UndefinedTable", "NoSuchTableError"})

# Driver-message markers for a missing relation, matched case-insensitively
# against the DBAPI cause ONLY to classify (never emitted). SQLite reports a
# missing table as a bare OperationalError whose only signal is this text.
_MISSING_RELATION_MARKERS = ("no such table", "does not exist", "undefined table")


def _is_missing_relation_error(exc: BaseException) -> bool:
    """Whether ``exc`` means a queried table is absent (schema not migrated).

    Distinguishes "the database was never migrated" from a transient connection
    failure so an operator gets the right remedy — WITHOUT letting the raw error
    reach the caller. It reads the exception's CLASS NAMES (the SQLAlchemy wrapper
    and its ``.orig`` DBAPI cause) and, only as a fallback for drivers that carry
    no distinct class (SQLite), a small set of missing-relation marker substrings
    in the DBAPI cause's text.

    That text is consulted solely to classify here; it is never placed in a
    ``ReadinessDependency.detail``. ``/ready`` is anonymous-public and now rendered
    in a browser, so the raw ``[SQL: ...]``/parameters block SQLAlchemy bakes into
    ``str(exc)`` must never be emitted (note the ``.orig`` message alone excludes
    that block, but the detail is hand-written regardless).
    """
    names = {type(exc).__name__}
    orig = getattr(exc, "orig", None)
    if orig is not None:
        names.add(type(orig).__name__)
    if names & _MISSING_RELATION_ERROR_NAMES:
        return True
    lowered = str(orig).lower() if orig is not None else ""
    return any(marker in lowered for marker in _MISSING_RELATION_MARKERS)


async def probe_migrations(engine: AsyncEngine) -> ReadinessDependency:
    """Schema version — the DB must be migrated to the head this code expects.

    GATING, and deliberately so. Every other DB probe is satisfied by a
    reachable-but-EMPTY database: ``probe_document_store``'s ``SELECT 1`` needs no
    table, so a container pointed at a zero-table Postgres reported ``ready`` while
    every chat/rounds/document request 500'd on the first query. DEPLOY.md §15/§18
    make ``alembic upgrade head`` an explicit MANUAL step *after* ``up -d``, so the
    unmigrated window is a documented, routine part of the rollout — not an exotic
    failure. Readiness must describe it honestly.

    Not-ready on any of: no ``alembic_version`` table (never migrated), an empty
    ``alembic_version``, or an applied revision set that differs from the code's
    head(s) (a migration was added and not applied — or the image was rolled back
    behind the DB). The detail always names the concrete revisions so an operator
    reads "run alembic upgrade head", not a bare exception class.
    """
    try:
        heads = set(script_directory().get_heads())
    except Exception as exc:
        # Scripts unreadable ⇒ we cannot prove the schema is current, so we must
        # not claim ready. Name the real error, not its class.
        return ReadinessDependency(
            name="migrations", ok=False, detail=f"cannot read migration scripts: {exc}"
        )

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            applied = {str(row[0]) for row in result}
    except Exception as exc:
        # SECURITY: never interpolate str(exc) here. SQLAlchemy fills it with the
        # raw "[SQL: SELECT version_num FROM alembic_version] [parameters: ...]"
        # block, and /ready is anonymous-public (and now rendered in a browser),
        # so emitting it leaks SQL to the world — violating "never expose SQL/paths".
        # Classify by exception TYPE and hand-write a safe cause instead.
        if _is_missing_relation_error(exc):
            cause = "no alembic_version table — the database has not been migrated"
        else:
            cause = (
                f"could not query alembic_version ({type(exc).__name__}) — "
                f"the database is unreachable or not yet migrated"
            )
        return ReadinessDependency(
            name="migrations",
            ok=False,
            detail=f"{cause}; run 'alembic upgrade head' (DEPLOY.md §18 step 3)",
        )

    if not applied:
        return ReadinessDependency(
            name="migrations",
            ok=False,
            detail=(
                f"no migration applied (alembic_version is empty); code head "
                f"{sorted(heads)} — run 'alembic upgrade head'"
            ),
        )
    if applied != heads:
        return ReadinessDependency(
            name="migrations",
            ok=False,
            detail=(
                f"schema at {sorted(applied)} but code expects head {sorted(heads)} — "
                f"run 'alembic upgrade head'"
            ),
        )
    return ReadinessDependency(name="migrations", ok=True, detail=f"at head {sorted(heads)}")


async def probe_smart_config(settings: Settings) -> ReadinessDependency:
    """SMART login config — in smart mode, login must actually be reachable.

    GATING. ``auth_mode=smart`` is the DEPLOYED mode, and in it login is the only
    way to obtain a session: an authorize URL the physician's browser cannot
    resolve means the service serves nobody, however green its other deps look.
    The shipped default (``http://openemr/oauth2/default/authorize``) is exactly
    that — an internal Docker alias — and an operator who enables SMART without
    overriding it gets /health 200, /ready 200, and a dead sign-in.

    Why here and not in ``create_app``: the boot gate (``ensure_smart_ready``)
    covers what is fatal to the whole process. The authorize URL is fatal only to
    the login flow, and the app is legitimately constructible in smart mode
    without it (delegated-token tests drive a seeded session and never redirect a
    browser). ``/ready`` is the operator's dashboard and DEPLOY.md's documented
    verification step, so a broken login config belongs there — visible and
    gating, without making an in-process app un-buildable.

    A no-op ``ok`` outside smart mode: nothing redirects a browser when auth is
    disabled, so the authorize URL is unused and its value irrelevant.
    """
    if settings.auth_mode != "smart":
        return ReadinessDependency(
            name="smart_config", ok=True, detail=f"auth_mode={settings.auth_mode} (login off)"
        )
    try:
        ensure_authorize_url_browser_reachable(settings)
    except AuthConfigError as exc:
        return ReadinessDependency(name="smart_config", ok=False, detail=str(exc))
    return ReadinessDependency(
        name="smart_config", ok=True, detail="smart login config valid (authorize URL public https)"
    )


async def probe_postgres(engine: AsyncEngine) -> ReadinessDependency:
    """`SELECT 1` — proves the URL works and the pool can hand out a conn."""
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            _ = result.scalar_one()
        return ReadinessDependency(name="postgres", ok=True)
    except Exception as exc:
        return ReadinessDependency(name="postgres", ok=False, detail=type(exc).__name__)


async def probe_document_store(engine: AsyncEngine) -> ReadinessDependency:
    """Agent document store — the Postgres that holds source_document / pages / facts.

    A ``SELECT 1`` proves the URL works and the pool can hand out a connection;
    the ingestion pipeline persists every derived artifact here, so it is a
    gating dependency (a down store means uploads cannot be recorded).
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            _ = result.scalar_one()
        return ReadinessDependency(name="document_store", ok=True, detail="reachable")
    except Exception as exc:
        return ReadinessDependency(name="document_store", ok=False, detail=type(exc).__name__)


async def probe_pgvector(settings: Settings, engine: AsyncEngine) -> ReadinessDependency:
    """pgvector availability — dense guideline retrieval needs the vector extension.

    On Postgres the ``vector`` extension must be installed for ANN search; a
    missing extension is a real degradation (dense retrieval falls back to
    sparse-only), so it is reported advisory — sparse retrieval still serves. On
    SQLite (tests / keyless dev) the JSON fallback column is used, which is
    always available, so the probe reports ready.
    """
    if not settings.database_url.startswith("postgresql"):
        return ReadinessDependency(
            name="pgvector", ok=True, detail="sqlite json-vector fallback", advisory=True
        )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            installed = result.first() is not None
        if installed:
            return ReadinessDependency(name="pgvector", ok=True, detail="extension installed")
        return ReadinessDependency(
            name="pgvector", ok=False, detail="vector extension not installed", advisory=True
        )
    except Exception as exc:
        return ReadinessDependency(
            name="pgvector", ok=False, detail=type(exc).__name__, advisory=True
        )


async def probe_guideline_corpus(engine: AsyncEngine) -> ReadinessDependency:
    """Guideline corpus population — an empty corpus serves zero evidence.

    The migrations create ``guideline_chunk`` empty; the corpus is loaded by a
    separate, manual step (``scripts/ingest_guidelines.py`` — DEPLOY.md §18
    step 4). Skipping it leaves hybrid retrieval structurally unable to return
    anything while every other dependency reports healthy, so the deployment
    looks ready and silently answers with no evidence.

    Advisory on every branch, like ``probe_langfuse``: an empty corpus is a real
    degradation but the service still serves (chat answers from FHIR facts; the
    retriever returns ``[]``, which is honest no-evidence rather than a
    fabricated cite), so it must never 503 the deployment out of rotation.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(select(func.count()).select_from(GuidelineChunkRow))
            count = int(result.scalar_one() or 0)
    except Exception as exc:
        # SECURITY: never interpolate str(exc). This probe is the only one that
        # queries an application table, so on an unmigrated DB it is the first line
        # to break — but SQLAlchemy's str(exc) carries the raw "[SQL: ... FROM
        # guideline_chunk]" block, and /ready is anonymous-public (and now
        # browser-rendered). Name the actual rollout mistake by classifying the
        # exception TYPE, not by echoing the raw error.
        if _is_missing_relation_error(exc):
            detail = (
                "guideline_chunk table missing — the database has not been migrated; "
                "run 'alembic upgrade head' (DEPLOY.md §18 step 3)"
            )
        else:
            detail = f"guideline_chunk unreadable ({type(exc).__name__})"
        return ReadinessDependency(
            name="guideline_corpus", ok=False, detail=detail, advisory=True
        )
    if count == 0:
        return ReadinessDependency(
            name="guideline_corpus",
            ok=False,
            detail=(
                "empty corpus — guideline retrieval returns no evidence; run "
                "scripts/ingest_guidelines.py (DEPLOY.md §18 step 4)"
            ),
            advisory=True,
        )
    return ReadinessDependency(
        name="guideline_corpus", ok=True, detail=f"{count} chunks", advisory=True
    )


async def probe_embedder(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """Guideline embedder — Voyage when keyed, the deterministic stub otherwise.

    Reachability, not mere config presence. A set key pointed at a dead backend is
    NOT ok — mirroring ``probe_llm``, a configured key triggers a real reachability
    call. Voyage exposes no cheap models-list GET, so the lightest genuine
    authenticated primitive is used: a one-token ``POST /v1/embeddings`` with the
    configured model (short timeout). A well-formed ``200`` reports ``ok``; a
    keyed-but-unreachable/erroring endpoint reports ``degraded`` (never a silent
    ``ok``), naming the reason.

    Advisory on EVERY branch, like ``probe_langfuse``: a Voyage outage degrades
    dense retrieval to the lexical stub — it must never pull the service out of
    rotation, so this can be ``degraded`` but never turns ``/ready`` into a 503.

    Keyless is a supported mode (the deterministic stub embeds offline) and makes
    NO network call — there is nothing to ping — reporting the stub as ``ok`` so a
    dashboard can see it is running keyless.
    """
    if not settings.voyage_api_key:
        return ReadinessDependency(
            name="embedder", ok=True, detail="stub (keyless)", advisory=True
        )
    backend = f"voyage:{settings.voyage_embedding_model}"
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = "https://api.voyageai.com/v1/embeddings"
    headers = {"Authorization": f"Bearer {settings.voyage_api_key}"}
    payload = {"input": ["ping"], "model": settings.voyage_embedding_model}
    try:
        async with factory() as client:
            resp = await client.post(url, json=payload, headers=headers)
        # As with probe_llm, an empty/opaque 200 does not prove the embedding
        # backend is up; require a well-formed JSON body.
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(
                name="embedder", ok=True, detail=f"{backend} reachable", advisory=True
            )
        return ReadinessDependency(
            name="embedder",
            ok=False,
            detail=f"{backend} unreachable (status={resp.status_code})",
            advisory=True,
        )
    except Exception as exc:
        return ReadinessDependency(
            name="embedder",
            ok=False,
            detail=f"{backend} unreachable ({type(exc).__name__})",
            advisory=True,
        )


async def probe_reranker(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """Retrieval reranker — Cohere when keyed, the deterministic stub otherwise.

    Reachability, not mere config presence. Mirroring ``probe_llm``, a configured
    key triggers a real reachability call: a short authenticated
    ``GET /v1/models`` — the cheapest real Cohere endpoint, which runs no rerank.
    A well-formed ``200`` reports ``ok``; a keyed-but-unreachable/erroring endpoint
    reports ``degraded`` (never a silent ``ok``), naming the reason.

    Advisory on EVERY branch, like ``probe_langfuse``: a Cohere outage costs only a
    ranking refinement the retriever already falls back from (fused sparse+dense
    order), never the answer — so this can be ``degraded`` but never 503s
    ``/ready``.

    Keyless is a supported mode (fused order served without a rerank) and makes NO
    network call, reporting the stub as ``ok``.
    """
    if not settings.cohere_api_key:
        return ReadinessDependency(
            name="reranker", ok=True, detail="stub (keyless)", advisory=True
        )
    backend = f"cohere:{settings.cohere_rerank_model}"
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = "https://api.cohere.com/v1/models"
    headers = {"Authorization": f"Bearer {settings.cohere_api_key}"}
    try:
        async with factory() as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(
                name="reranker", ok=True, detail=f"{backend} reachable", advisory=True
            )
        return ReadinessDependency(
            name="reranker",
            ok=False,
            detail=f"{backend} unreachable (status={resp.status_code})",
            advisory=True,
        )
    except Exception as exc:
        return ReadinessDependency(
            name="reranker",
            ok=False,
            detail=f"{backend} unreachable ({type(exc).__name__})",
            advisory=True,
        )


async def probe_openemr_fhir(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """`GET {fhir_base}/metadata` — CapabilityStatement is public, no auth needed."""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=5.0))
    url = settings.fhir_base_url.rstrip("/") + "/metadata"
    try:
        async with factory() as client:
            resp = await client.get(url)
        if resp.status_code == 200 and "CapabilityStatement" in resp.text[:200]:
            return ReadinessDependency(name="openemr_fhir", ok=True)
        return ReadinessDependency(
            name="openemr_fhir", ok=False, detail=f"status={resp.status_code}"
        )
    except Exception as exc:
        return ReadinessDependency(name="openemr_fhir", ok=False, detail=type(exc).__name__)


async def probe_llm(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """LLM readiness — the provider must be *reachable*, not merely configured.

    A set key pointed at a dead backend is not ready: we attempt a short
    ``GET {anthropic_base_url}/v1/models`` with the key. Any HTTP response
    (even 401 for a bad key) proves the endpoint answered, so the provider is
    reachable; only a transport failure (refused/DNS/timeout) is not-ready.
    Absence of the key stays not-ready without a network call — there is
    nothing to ping.
    """
    if not settings.anthropic_api_key:
        return ReadinessDependency(name="llm", ok=False, detail="ANTHROPIC_API_KEY not set")
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = settings.anthropic_base_url.rstrip("/") + "/v1/models"
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        async with factory() as client:
            resp = await client.get(url, headers=headers)
        # Reachable == the real provider answered with a well-formed 200 payload.
        # A bare/empty 200 (a dead port, a catch-all proxy) is NOT proof the LLM
        # backend is up — mirror probe_openemr_fhir, which validates content, not
        # just status.
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(name="llm", ok=True, detail="reachable")
        return ReadinessDependency(name="llm", ok=False, detail=f"status={resp.status_code}")
    except Exception as exc:
        return ReadinessDependency(name="llm", ok=False, detail=type(exc).__name__)


async def probe_langfuse(
    settings: Settings, client_factory: Callable[[], httpx.AsyncClient] | None = None
) -> ReadinessDependency:
    """Langfuse readiness — reachability, but advisory (never gating).

    Observability is valuable but must not pull the service out of rotation, so
    the result is always flagged ``advisory``: a failing Langfuse never turns
    ``/ready`` into a 503. Semantics still verify reachability rather than mere
    credential presence — with all three creds set we ping the host's public
    health endpoint (short timeout); a transport failure reports not-ok.
    """
    configured = bool(
        settings.langfuse_host and settings.langfuse_public_key and settings.langfuse_secret_key
    )
    if not configured:
        return ReadinessDependency(
            name="langfuse", ok=False, detail="not configured (advisory)", advisory=True
        )
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    url = settings.langfuse_host.rstrip("/") + "/api/public/health"
    try:
        async with factory() as client:
            resp = await client.get(url)
        # As with the LLM probe, an empty/opaque 200 does not prove Langfuse is
        # actually up; require a well-formed JSON health payload.
        if resp.status_code == 200 and isinstance(resp.json(), dict):
            return ReadinessDependency(name="langfuse", ok=True, detail="reachable", advisory=True)
        return ReadinessDependency(
            name="langfuse", ok=False, detail=f"status={resp.status_code}", advisory=True
        )
    except Exception as exc:
        return ReadinessDependency(
            name="langfuse", ok=False, detail=type(exc).__name__, advisory=True
        )


async def run_all(
    probes: list[Callable[[], Awaitable[ReadinessDependency]]],
) -> list[ReadinessDependency]:
    """Run probes sequentially — order preserved for stable JSON output.

    Kept sequential rather than gathered so a slow probe doesn't blur
    which dependency caused the delay in traces.
    """
    return [await p() for p in probes]
