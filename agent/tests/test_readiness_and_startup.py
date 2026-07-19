"""Two operability guarantees that were documented but not real.

1. **/ready must not be green on an unmigrated database.** Every DB probe before
   ``probe_migrations`` was satisfied by a *reachable* store: ``SELECT 1`` needs no
   table. DEPLOY.md §15/§18 make ``alembic upgrade head`` a MANUAL step after
   ``up -d``, so "reachable but zero tables" is a routine window in the documented
   rollout — and in it the container reported healthy, /ready reported ready, and
   every chat/rounds/document request 500'd.

2. **create_app must refuse an unsafe SMART config.** config.py and DEPLOY.md §16.3
   both promise "a startup check refuses auth_mode=smart without an https origin" /
   "refuses to boot". No such check ran at startup — ``ensure_smart_ready`` was
   called only on the first login, so a bad config booted green and died at sign-in.

The last test in each half is the regression guard: without it, "never ready" and
"never boots" would pass for entirely the wrong reason.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from functools import partial
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from copilot.api import readiness
from copilot.api.app import _default_probe_factories, create_app
from copilot.auth.service import AuthConfigError
from copilot.config import Settings
from copilot.domain.contracts import ReadinessDependency

_ROOT = Path(__file__).resolve().parents[1]


# --- helpers ----------------------------------------------------------------


def _unmigrated_engine(tmp_path: Path) -> AsyncEngine:
    """A reachable SQLite DB with ZERO tables — the pre-`alembic upgrade` droplet."""
    return create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'unmigrated.db'}")


def _alembic_config(db_file: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("COPILOT_DATABASE_URL", f"sqlite+aiosqlite:///{db_file}")
    from copilot.config import get_settings

    get_settings.cache_clear()  # env.py resolves the URL through Settings
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    return cfg


# `command.upgrade` runs alembic's env.py, which calls `asyncio.run()` — illegal
# inside a running loop. So the migration happens in SYNC fixtures (pytest runs
# those outside the async test's loop) and the async tests receive a path.


@pytest.fixture
def migrated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DB after the documented `alembic upgrade head` step."""
    db_file = tmp_path / "migrated.db"
    command.upgrade(_alembic_config(db_file, monkeypatch), "head")
    return f"sqlite+aiosqlite:///{db_file}"


@pytest.fixture
def stale_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A DB exactly one revision behind the code's head."""
    db_file = tmp_path / "stale.db"
    cfg = _alembic_config(db_file, monkeypatch)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    return f"sqlite+aiosqlite:///{db_file}"


def _ready_client(*probes: Callable[[], Awaitable[ReadinessDependency]]) -> TestClient:
    factories = [lambda _s, p=p: p for p in probes]  # type: ignore[misc]
    return TestClient(create_app(settings=Settings(), probe_factories=factories))


def _smart_settings(**overrides: object) -> Settings:
    """A CORRECT smart config, shaped like the live droplet's. Override to break it."""
    base: dict[str, object] = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "auth_mode": "smart",
        "public_base_url": "https://agentforge.example.com",
        "session_enc_key": "unit-enc-key",
        "smart_app_client_id": "login-client",
        "smart_app_client_secret": "login-secret",
        "oauth_authorize_url": "https://agentforge.example.com/oauth2/default/authorize",
        "oauth_token_url": "http://openemr/oauth2/default/token",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _restore_settings_cache() -> Iterator[None]:
    from copilot.config import get_settings

    yield
    get_settings.cache_clear()


# --- 1. readiness on an unmigrated DB ---------------------------------------


async def test_unmigrated_db_is_not_ready(tmp_path: Path) -> None:
    """The exact observed defect: reachable DB, zero tables, /ready said 200/ready."""
    engine = _unmigrated_engine(tmp_path)
    try:
        client = _ready_client(partial(readiness.probe_migrations, engine))
        resp = client.get("/ready")
        assert resp.status_code == 503
        body = resp.json()
        assert body["ready"] is False
        dep = next(d for d in body["dependencies"] if d["name"] == "migrations")
        assert dep["ok"] is False
        assert dep["advisory"] is False, "migrations must GATE readiness, not advise"
        assert dep["status"] == "down"
    finally:
        await engine.dispose()


async def test_unmigrated_db_detail_tells_the_operator_what_to_run(tmp_path: Path) -> None:
    """A rollout mistake must read as one — not as an opaque exception class."""
    engine = _unmigrated_engine(tmp_path)
    try:
        dep = await readiness.probe_migrations(engine)
    finally:
        await engine.dispose()
    assert dep.ok is False
    assert "alembic upgrade head" in dep.detail
    # Names the real cause (the missing alembic_version table / unmigrated DB) — not
    # just an opaque exception class — WITHOUT leaking the raw SQL query (security).
    assert "alembic_version" in dep.detail
    assert "migrated" in dep.detail
    assert "SELECT" not in dep.detail and "[SQL:" not in dep.detail, (
        "must not leak the raw SQL query into a public /ready detail"
    )


async def test_migrated_db_is_ready(migrated_db: str) -> None:
    """REGRESSION GUARD: without this, a probe that never passes would look correct."""
    engine = create_async_engine(migrated_db)
    try:
        client = _ready_client(partial(readiness.probe_migrations, engine))
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        dep = next(d for d in body["dependencies"] if d["name"] == "migrations")
        assert dep["ok"] is True
        assert "at head" in dep["detail"]
    finally:
        await engine.dispose()


async def test_stale_schema_behind_code_head_is_not_ready(stale_db: str) -> None:
    """The 'added a migration, forgot to apply it' case: schema one revision behind."""
    engine = create_async_engine(stale_db)
    try:
        dep = await readiness.probe_migrations(engine)
    finally:
        await engine.dispose()
    assert dep.ok is False
    assert dep.advisory is False
    assert "code expects head" in dep.detail


async def test_guideline_corpus_detail_names_the_real_error(tmp_path: Path) -> None:
    """The advisory line that was the ONLY clue was printing 'OperationalError'."""
    engine = _unmigrated_engine(tmp_path)
    try:
        dep = await readiness.probe_guideline_corpus(engine)
    finally:
        await engine.dispose()
    assert dep.ok is False
    # Names the real cause (the missing guideline_chunk table / unmigrated DB) — not
    # just an opaque exception class — WITHOUT leaking the raw SQL query (security).
    assert "guideline_chunk" in dep.detail
    assert "migrated" in dep.detail
    assert "SELECT" not in dep.detail and "[SQL:" not in dep.detail, (
        "must not leak the raw SQL query into a public /ready detail"
    )


def test_default_probe_set_wires_migrations_as_a_gating_probe() -> None:
    """Guards the wiring, not just the probe.

    A perfect probe that ``create_app`` never calls leaves the defect live, so
    assert the real default factory list actually contains it.
    """
    probes = [factory(Settings()) for factory in _default_probe_factories()]
    assert any(getattr(p, "func", None) is readiness.probe_migrations for p in probes), (
        "create_app's default probes must include probe_migrations"
    )


def test_default_probe_set_wires_smart_config() -> None:
    """Same wiring guard for the login-reachability gate."""
    probes = [factory(Settings()) for factory in _default_probe_factories()]
    assert any(getattr(p, "func", None) is readiness.probe_smart_config for p in probes), (
        "create_app's default probes must include probe_smart_config"
    )


# --- 2. the startup check that did not exist --------------------------------


def test_create_app_refuses_smart_without_https_origin() -> None:
    with pytest.raises(AuthConfigError, match="https public_base_url"):
        create_app(settings=_smart_settings(public_base_url="http://af.test"), probe_factories=[])


def test_create_app_refuses_smart_without_client_secret() -> None:
    """The confidential login client's secret — unchecked, so login died at exchange."""
    with pytest.raises(AuthConfigError, match="smart_app_client_secret"):
        create_app(settings=_smart_settings(smart_app_client_secret=""), probe_factories=[])


# --- 3. the unreachable authorize URL: gated at /ready, not at boot ---------
#
# The brief asked for a BOOT check rejecting an oauth_authorize_url whose host
# differs from public_base_url's. That rule is not shippable, on two counts:
#
#   1. It contradicts this repo's own suite. EVERY existing smart-mode fixture is
#      split-host — app on `af.test`, authorize on `openemr.test`
#      (test_auth_routes.py, test_auth_cutover_routes.py). Host-equality reddens
#      all of them, and they may not be edited.
#   2. It is wrong in general. Standard SMART on FHIR routinely puts the app and
#      the EHR's authorization server on different hosts. Same-host is a property
#      of THIS deployment's Caddy topology, not a protocol invariant.
#
# And any BOOT check on the authorize URL — host-equality or the browser-
# reachability rule used below — reddens test_delegated_token_cutover.py, which
# boots smart mode on the DEFAULT authorize URL because it drives delegated-token
# reads/writes against a seeded session and never redirects a browser. That test
# is right: the app IS constructible in smart mode without a login redirect.
#
# So the real defect (a browser sent to the internal `http://openemr`) is caught
# where it is both true and observable: a GATING /ready dependency + a loud
# failure at begin_login. /ready is the operator's dashboard and DEPLOY.md's
# documented verification step.


def _smart_config_dep(settings: Settings) -> ReadinessDependency:
    client = TestClient(
        create_app(
            settings=settings,
            probe_factories=[lambda s: partial(readiness.probe_smart_config, s)],
        )
    )
    body = client.get("/ready").json()
    return ReadinessDependency(**body["dependencies"][0])


def test_ready_is_red_on_internal_authorize_url() -> None:
    """The observed defect: the physician's browser sent to an internal Docker host.

    ``http://openemr/oauth2/default/authorize`` is the shipped DEFAULT, so this is
    exactly what an operator following the old .env.deploy.example got — while
    /ready reported 200.
    """
    settings = _smart_settings(oauth_authorize_url="http://openemr/oauth2/default/authorize")
    client = TestClient(
        create_app(
            settings=settings,
            probe_factories=[lambda s: partial(readiness.probe_smart_config, s)],
        )
    )
    resp = client.get("/ready")
    assert resp.status_code == 503
    dep = resp.json()["dependencies"][0]
    assert dep["name"] == "smart_config"
    assert dep["ok"] is False
    assert dep["advisory"] is False, "a login flow nobody can reach must GATE readiness"
    assert "COPILOT_OAUTH_AUTHORIZE_URL" in dep["detail"], "name the knob to set"


def test_ready_is_red_on_plaintext_authorize_url() -> None:
    """https is the guard's whole premise; a plaintext redirect leaks state/PKCE."""
    dep = _smart_config_dep(
        _smart_settings(oauth_authorize_url="http://openemr.example.com/oauth2/default/authorize")
    )
    assert dep.ok is False
    assert "https oauth_authorize_url" in dep.detail


def test_ready_is_red_on_single_label_authorize_host() -> None:
    """https alone is not enough — a container alias still resolves nowhere."""
    dep = _smart_config_dep(
        _smart_settings(oauth_authorize_url="https://openemr/oauth2/default/authorize")
    )
    assert dep.ok is False
    assert "publicly-resolvable" in dep.detail


def test_ready_is_green_on_the_live_shaped_smart_config() -> None:
    """REGRESSION GUARD — mirrors the deployed droplet's authorize URL."""
    assert _smart_config_dep(_smart_settings()).ok is True


def test_ready_smart_config_is_a_noop_when_auth_disabled() -> None:
    """Nothing redirects a browser in disabled mode, so the URL is irrelevant."""
    dep = _smart_config_dep(
        Settings(
            database_url="sqlite+aiosqlite:///:memory:",
            auth_mode="disabled",
            oauth_authorize_url="http://openemr/oauth2/default/authorize",
        )
    )
    assert dep.ok is True


async def test_begin_login_refuses_an_unreachable_authorize_url() -> None:
    """Login fails LOUDLY naming the knob — it must not redirect a browser to a dead host."""
    from copilot.auth.service import AuthService

    svc = AuthService(
        _smart_settings(oauth_authorize_url="http://openemr/oauth2/default/authorize")
    )
    with pytest.raises(AuthConfigError, match="COPILOT_OAUTH_AUTHORIZE_URL"):
        await svc.begin_login()


def test_create_app_boots_on_a_correct_smart_config() -> None:
    """REGRESSION GUARD — and the check that the LIVE droplet still boots.

    Mirrors the deployed config's shape: https public origin, session key, client
    id + secret, and a PUBLIC https authorize endpoint on the same origin (Caddy
    proxies /oauth2/* to OpenEMR). A guard that refuses this would take the live
    service down on the next deploy.
    """
    app = create_app(settings=_smart_settings(), probe_factories=[])
    assert TestClient(app).get("/health").status_code == 200


def test_create_app_tolerates_split_host_authorize_endpoint() -> None:
    """Standard SMART: the EHR's authorization server need not share the app's host.

    Encoding same-host as a boot requirement would reject a legitimate deployment
    (and contradicts the existing auth tests, which run app-on-af.test against
    authorize-on-openemr.test).
    """
    app = create_app(
        settings=_smart_settings(
            oauth_authorize_url="https://ehr.example.org/oauth2/default/authorize"
        ),
        probe_factories=[],
    )
    assert TestClient(app).get("/health").status_code == 200


def test_disabled_mode_boots_unchanged() -> None:
    """REGRESSION GUARD: the guard is a no-op outside smart mode.

    auth_mode=disabled with a plaintext origin and no SMART creds is the default
    demo — it must keep booting byte-for-byte.
    """
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        auth_mode="disabled",
        public_base_url="http://198.199.68.21",
        oauth_authorize_url="http://openemr/oauth2/default/authorize",
    )
    app = create_app(settings=settings, probe_factories=[])
    assert TestClient(app).get("/health").status_code == 200
