"""feat_api criterion 5 — graded `/ready`: probes document_store / pgvector /
embedder / reranker; a degraded dependency is REFLECTED in the graded payload
(not a bare 503, not a crash); `/health` stays liveness-only.

FROZEN GOALS, black-box over HTTP. "Graded" contract pinned here: every probed
dependency entry carries a string state field (``status``/``state``/``grade``)
in addition to the Week-1 ``ok`` boolean, so degraded-but-serving is
distinguishable from down.
"""

from __future__ import annotations

REQUIRED_DEPS = {"document_store", "pgvector", "embedder", "reranker"}
GRADE_FIELDS = ("status", "state", "grade")
HEALTHY_GRADES = {"ok", "up", "ready", "healthy"}


def _dep_index(payload: dict) -> dict:
    deps = payload.get("dependencies", [])
    return {d.get("name"): d for d in deps if isinstance(d, dict)}


def _grade_of(entry: dict) -> str | None:
    return next(
        (entry[f] for f in GRADE_FIELDS if isinstance(entry.get(f), str)), None
    )


def test_api_05_graded_ready_probes_and_liveness_health(make_client):
    # Real probe wiring (probe_factories=None -> the app's defaults). Keyless
    # stub env: probes must still enumerate and grade every dependency.
    client = make_client(probe_factories=None)
    r = client.get("/ready")
    assert r.status_code in (200, 503), (
        f"/ready must answer with a graded readiness payload; got {r.status_code}"
    )
    payload = r.json()
    deps = _dep_index(payload)
    missing = REQUIRED_DEPS - set(deps)
    assert not missing, (
        f"/ready must probe {sorted(REQUIRED_DEPS)} (Week-2 graded readiness); "
        f"missing {sorted(missing)}; probed: {sorted(k for k in deps if k)}"
    )
    for name in sorted(REQUIRED_DEPS):
        grade = _grade_of(deps[name])
        assert grade, (
            f"dependency {name!r} must carry a graded string state (one of "
            f"{GRADE_FIELDS}) so degraded != down; entry: {deps[name]}"
        )

    # A failing dependency must be reflected as degraded/down in the payload,
    # while the payload still enumerates it (graded, fail-visible, no crash).
    from copilot.domain.contracts import ReadinessDependency

    async def _down() -> ReadinessDependency:
        return ReadinessDependency(
            name="document_store", ok=False, detail="injected: document store unreachable"
        )

    degraded_client = make_client(probe_factories=[lambda s: _down])
    dr = degraded_client.get("/ready")
    assert dr.status_code in (200, 503), f"degraded /ready -> {dr.status_code}"
    dentry = _dep_index(dr.json()).get("document_store")
    assert dentry is not None, (
        "the graded payload must still enumerate a failing dependency"
    )
    dgrade = _grade_of(dentry)
    reflected = dentry.get("ok") is False or (
        dgrade is not None and dgrade.lower() not in HEALTHY_GRADES
    )
    assert reflected, (
        f"a failing probe must surface as degraded/down in the graded payload; "
        f"entry: {dentry}"
    )

    # /health stays liveness-only: 200, no dependency probing in the body.
    h = client.get("/health")
    assert h.status_code == 200, f"/health must stay pure liveness; got {h.status_code}"
    hbody = h.json()
    assert isinstance(hbody, dict) and "dependencies" not in hbody, (
        f"/health must not enumerate dependencies (liveness only); got {hbody}"
    )
