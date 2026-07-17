"""Contract tests: the committed OpenAPI spec must match the implementation.

WHY THIS EXISTS
---------------
``agent/openapi/week2.yaml`` is a published deliverable ("publish an OpenAPI 3
spec for all Week 2 HTTP endpoints, kept in sync with the implementation").
A committed spec that nothing loads is decoration — it rots silently the first
time somebody adds a route or renames a response field. These tests are the
enforcement: they fail the normal ``pytest`` run (and therefore CI) the moment
the committed spec drifts from what the app generates.

Note there is a parallel check in the frozen acceptance harness
(``.swarm-loop/acceptance/api/test_api_04_openapi_sync.py``). That harness is
NOT in pytest's ``testpaths`` (see ``pyproject.toml``), so it never runs during
a normal ``pytest`` / CI invocation. This file is the project suite's own
contract test and stands on its own.

WHAT THESE TESTS PROVE — AND WHAT THEY DO NOT
---------------------------------------------
PROVE: the committed YAML is byte-for-byte semantically identical to the app's
*self-description* (``create_app(...).openapi()``) — same paths, same methods,
same request/response schema wiring, same component schemas.

DO NOT PROVE: that the app's *runtime behavior* matches either document. FastAPI
derives the schema from route signatures and response models, so this is a check
of declaration-vs-declaration, not declaration-vs-reality. A handler that
declares ``response_model=Foo`` and then returns a hand-built ``JSONResponse``
with entirely different keys will pass every test in this file. Nothing here
sends a request, and nothing validates a real payload against the schema.
Behavioral coverage lives in the per-route tests (``test_chat_routes.py``,
``test_writes_route.py``, ``test_health_ready.py``, …). Do not read a green run
here as "the API behaves as documented" — read it as "the documented API and the
declared API agree".

OPENAPI 3.0 vs 3.1 — WHY THIS SPEC DECLARES 3.1.0
-------------------------------------------------
The requirement says "OpenAPI 3.0 / Swagger definitions". We publish
``openapi: 3.1.0`` deliberately, and ``test_openapi_version_is_pinned_to_3_1_0``
below guards that so it cannot change silently.

Rationale — relabeling this document "3.0.x" would make it *invalid*, not
compliant. FastAPI 0.115 + Pydantic v2 emit JSON Schema 2020-12 constructs that
OpenAPI 3.0 cannot express, and the committed spec is full of them:

* ``exclusiveMinimum: 0`` — a NUMBER (JSON Schema 2020-12 / OAS 3.1). In OAS 3.0
  ``exclusiveMinimum`` is a BOOLEAN modifier on ``minimum``. Every ``Field(gt=0)``
  on our ID primitives emits the numeric form: 27 occurrences as committed.
* ``type: 'null'`` inside ``anyOf`` — OAS 3.0 has no null type; it spells this
  ``nullable: true``. Every ``X | None`` field emits the 3.1 form: 28 occurrences
  as committed, against zero uses of ``nullable:``.

FastAPI offers no honest down-conversion here (verified against the pinned
fastapi 0.115.14 / pydantic 2.13):

* ``FastAPI.__init__`` does not accept an ``openapi_version`` argument at all —
  passing one is silently swallowed by ``**extra`` and ignored.
* ``fastapi.openapi.utils.get_openapi`` does accept ``openapi_version``, but does
  nothing with it except stamp it into the ``openapi:`` field.
* Overriding ``app.openapi_version = "3.0.3"`` (the only mechanism that has any
  effect) was measured: the header changes and the body does not — all 55
  JSON-Schema-2020-12 constructs remain, and no ``nullable:`` appears.

So "pinning to 3.0" relabels the header and fixes nothing, yielding a document
that claims a version it provably violates — which would BREAK the strict 3.0
tooling the relabel was meant to serve, since that tooling would then parse a
numeric ``exclusiveMinimum`` under 3.0 rules. An accurate 3.1.0 label is better
than a false 3.0 one. Real 3.0 support would need a converter that rewrites the
constructs, not a version string.

Accepted trade-off: OAS 3.1 is a superset of JSON Schema, and some 3.0-only
tooling (older Swagger UI builds, some codegen) cannot read it. Consumers that
need 3.0 should down-convert at the boundary with a real converter, which
rewrites the constructs rather than just the version string.

MAINTENANCE
-----------
These tests never require hand-editing the YAML. On any route or response-model
change, regenerate it::

    python scripts/dump_openapi.py
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from copilot.api.app import create_app
from copilot.config import get_settings

AGENT_DIR = Path(__file__).resolve().parents[1]
SPEC_PATH = AGENT_DIR / "openapi" / "week2.yaml"

REGEN = "regenerate it with `python scripts/dump_openapi.py` (run from agent/) and commit the result"

# HTTP verbs an OpenAPI Path Item may carry. Everything else at that level
# (`parameters`, `summary`, `$ref`, …) is not an operation.
_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


def _load_committed_spec() -> dict[str, Any]:
    """Parse the committed spec. Structural compare — YAML formatting is irrelevant."""
    assert SPEC_PATH.is_file(), (
        f"the OpenAPI spec must be committed at {SPEC_PATH.relative_to(AGENT_DIR)} — file is missing; {REGEN}"
    )
    spec = yaml.safe_load(SPEC_PATH.read_text())
    assert isinstance(spec, dict), (
        f"{SPEC_PATH.relative_to(AGENT_DIR)} must parse to an OpenAPI document object, got {type(spec).__name__}"
    )
    return spec


def _generated_schema() -> dict[str, Any]:
    """The app's self-described schema.

    Built exactly the way ``scripts/dump_openapi.py`` builds it, so a green test
    guarantees a no-op regeneration. ``probe_factories=[]`` keeps readiness
    probes from touching Postgres/OpenEMR; probes are runtime-only and
    contribute nothing to the schema.
    """
    return create_app(get_settings(), probe_factories=[]).openapi()


def _operations(doc: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """Flatten a document to ``{(path, method): operation}``."""
    ops: dict[tuple[str, str], dict[str, Any]] = {}
    paths = doc.get("paths") or {}
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, op in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(op, dict):
                ops[(path, method.lower())] = op
    return ops


def _iter_refs(node: Any) -> Iterator[str]:
    """Yield every ``$ref`` target anywhere beneath ``node``."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                yield value
            else:
                yield from _iter_refs(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_refs(item)


def _content_schemas(node: dict[str, Any]) -> dict[str, Any]:
    """``{media_type: schema}`` for a requestBody/response, schema exactly as declared."""
    return {mt: body.get("schema") for mt, body in (node.get("content") or {}).items()}


def _io_schemas(op: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What an operation wires to its request body and each response code.

    Keyed ``request`` / ``response:<code>``. This is the part a client actually
    codes against: change a response model and this changes with it.

    Captures the declared schema itself, not merely its ``$ref``. That matters
    because 7 of our operations (the ``/v1/auth/*`` routes and ``/status``)
    declare no ``response_model`` and so emit an INLINE untyped-object schema
    with no ``$ref`` anywhere — a ref-only comparison would be silently vacuous
    for exactly those routes. Comparing the declared schema keeps this check
    meaningful for every operation.
    """
    out: dict[str, dict[str, Any]] = {}
    if "requestBody" in op:
        out["request"] = _content_schemas(op["requestBody"])
    for code, response in (op.get("responses") or {}).items():
        out[f"response:{code}"] = _content_schemas(response)
    return out


def _describe(schemas: dict[str, Any] | None) -> str:
    """Compact, readable rendering of a slot's schemas for a failure message."""
    if schemas is None:
        return "ABSENT"
    if not schemas:
        return "no content"
    parts = []
    for mt, schema in sorted(schemas.items()):
        refs = sorted(set(_iter_refs(schema))) if schema is not None else []
        parts.append(f"{mt} -> {', '.join(refs) if refs else f'inline {schema!r}'}")
    return "; ".join(parts)


def _first_diffs(a: Any, b: Any, path: str = "$", out: list[str] | None = None, limit: int = 8) -> list[str]:
    """Up to ``limit`` human-readable paths at which two parsed documents differ.

    "committed" = agent/openapi/week2.yaml; "generated" = create_app().openapi().
    """
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append(f"{path}: committed is {type(a).__name__}, generated is {type(b).__name__}")
    elif isinstance(a, dict):
        for key in sorted(set(a) | set(b), key=str):
            if key not in a:
                out.append(f"{path}.{key}: MISSING from committed spec (app has it)")
            elif key not in b:
                out.append(f"{path}.{key}: STALE in committed spec (app no longer has it)")
            else:
                _first_diffs(a[key], b[key], f"{path}.{key}", out, limit)
            if len(out) >= limit:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: committed has {len(a)} items, generated has {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b, strict=True)):
                _first_diffs(x, y, f"{path}[{i}]", out, limit)
                if len(out) >= limit:
                    break
    elif a != b:
        out.append(f"{path}: committed {a!r} != generated {b!r}")
    return out


def test_openapi_version_is_pinned_to_3_1_0() -> None:
    """The declared OpenAPI version is a deliberate choice — it may not drift silently.

    See this module's docstring for the full defense. Short version: the body of
    this document uses JSON-Schema-2020-12 constructs (numeric ``exclusiveMinimum``,
    ``type: 'null'``) that OAS 3.0 cannot express, so 3.1.0 is the only accurate
    label. If you are changing this, you are changing a documented decision:
    update the module docstring and the architecture doc, not just the assertion.
    """
    spec = _load_committed_spec()
    generated = _generated_schema()

    assert spec["openapi"] == "3.1.0", (
        f"committed spec declares openapi {spec['openapi']!r}, expected '3.1.0'. "
        "This is a deliberate, documented decision (see this module's docstring): the schema body "
        "contains OAS-3.0-invalid constructs, so a 3.0.x label would be a false claim, not compliance. "
        f"If the app now genuinely emits 3.0, {REGEN} and update the docstring + architecture doc."
    )
    assert generated["openapi"] == "3.1.0", (
        f"the app now generates openapi {generated['openapi']!r}, not '3.1.0' — most likely a FastAPI "
        "upgrade changed the emitted dialect. Re-check the 3.0-vs-3.1 decision in this module's "
        f"docstring against what FastAPI now emits, then {REGEN}."
    )

    # The label must not outrun the body: these constructs are what make 3.1 the
    # honest answer. If they ever disappear, the decision is worth revisiting.
    text = SPEC_PATH.read_text()
    assert "type: 'null'" in text or "exclusiveMinimum: 0" in text, (
        "the committed spec no longer contains the JSON-Schema-2020-12 constructs that justify "
        "declaring 3.1.0 rather than 3.0.x. Revisit the decision recorded in this module's docstring — "
        "a 3.0 spec may now be achievable honestly."
    )


def test_committed_spec_documents_the_week2_surface() -> None:
    """Guard against a spec that is 'in sync' with an app that lost its routes.

    Every other test here compares committed-vs-generated, so deleting a route
    and re-dumping would keep them all green. These anchors assert the Week-2
    surface actually exists. Deliberately a SUBSET — the endpoints Week 2 is
    about (document ingestion, page images, chat, write-back) — so adding new
    routes never requires editing this list.
    """
    spec = _load_committed_spec()
    ops = _operations(spec)

    anchors = [
        ("/v1/documents", "post"),
        ("/v1/chat", "post"),
        ("/v1/writes", "post"),
    ]
    missing = [f"{m.upper()} {p}" for p, m in anchors if (p, m) not in ops]
    assert not missing, (
        f"the committed spec does not document the Week-2 surface: {', '.join(missing)} absent. "
        f"Either the route was removed from the app (a regression) or the spec is stale — if the routes exist, {REGEN}. "
        f"Documented operations: {sorted(f'{m.upper()} {p}' for p, m in ops)}"
    )

    assert any("/pages/" in path for path, _ in ops), (
        "the committed spec must document the page-image endpoint "
        f"(GET /v1/documents/{{document_id}}/pages/{{page_no}}); documented paths: {sorted(spec.get('paths') or {})}"
    )


def test_documented_paths_match_the_app() -> None:
    """Same set of URL paths in the committed spec and the app."""
    spec_paths = set(_load_committed_spec().get("paths") or {})
    app_paths = set(_generated_schema().get("paths") or {})

    undocumented = sorted(app_paths - spec_paths)
    stale = sorted(spec_paths - app_paths)
    assert not undocumented and not stale, (
        "agent/openapi/week2.yaml is OUT OF SYNC with the app's routes.\n"
        f"  Served by the app but UNDOCUMENTED (spec is stale): {undocumented or 'none'}\n"
        f"  Documented but NOT served (spec describes dead routes): {stale or 'none'}\n"
        f"Fix: {REGEN}."
    )


def test_documented_methods_match_the_app() -> None:
    """Same set of HTTP methods per path — catches e.g. an added PATCH."""
    spec_ops = set(_operations(_load_committed_spec()))
    app_ops = set(_operations(_generated_schema()))

    undocumented = sorted(f"{m.upper()} {p}" for p, m in app_ops - spec_ops)
    stale = sorted(f"{m.upper()} {p}" for p, m in spec_ops - app_ops)
    assert not undocumented and not stale, (
        "agent/openapi/week2.yaml documents the wrong set of operations.\n"
        f"  Served but UNDOCUMENTED: {undocumented or 'none'}\n"
        f"  Documented but NOT served: {stale or 'none'}\n"
        f"Fix: {REGEN}."
    )


def test_operation_request_and_response_schemas_match_the_app() -> None:
    """Each operation wires the same request/response schemas in spec and app.

    This is the contract clients code against: if POST /v1/chat starts returning
    a different model, or a 422 response appears, this fails naming the operation
    and the exact slot (request / response:<code>) that moved.
    """
    spec_ops = _operations(_load_committed_spec())
    app_ops = _operations(_generated_schema())

    problems: list[str] = []
    for path, method in sorted(spec_ops.keys() & app_ops.keys()):
        want = _io_schemas(spec_ops[(path, method)])
        got = _io_schemas(app_ops[(path, method)])
        for slot in sorted(set(want) | set(got)):
            if want.get(slot) != got.get(slot):
                problems.append(
                    f"{method.upper()} {path} [{slot}]:\n"
                    f"      committed: {_describe(want.get(slot))}\n"
                    f"      app:       {_describe(got.get(slot))}"
                )
    assert not problems, (
        "agent/openapi/week2.yaml wires different request/response schemas than the app.\n  "
        + "\n  ".join(problems)
        + f"\nFix: {REGEN}."
    )


def test_component_schemas_match_the_app() -> None:
    """Every referenced model is present and structurally identical.

    Catches field-level drift (renamed/added/removed/retyped properties) that the
    path- and ref-level checks above cannot see.
    """
    spec_schemas = (_load_committed_spec().get("components") or {}).get("schemas") or {}
    app_schemas = (_generated_schema().get("components") or {}).get("schemas") or {}

    undocumented = sorted(set(app_schemas) - set(spec_schemas))
    stale = sorted(set(spec_schemas) - set(app_schemas))
    assert not undocumented and not stale, (
        "agent/openapi/week2.yaml documents the wrong set of component schemas.\n"
        f"  Defined by the app but MISSING from the spec: {undocumented or 'none'}\n"
        f"  In the spec but GONE from the app: {stale or 'none'}\n"
        f"Fix: {REGEN}."
    )

    drifted = sorted(name for name in spec_schemas if spec_schemas[name] != app_schemas[name])
    if drifted:
        details = "; ".join(
            "; ".join(_first_diffs(spec_schemas[n], app_schemas[n], f"components.schemas.{n}", limit=4))
            for n in drifted[:3]
        )
        pytest.fail(
            f"component schema(s) drifted from the app's models: {drifted}. First diffs: {details}\nFix: {REGEN}."
        )


def test_committed_spec_is_exactly_what_the_app_generates() -> None:
    """Backstop: full structural equality, nothing excluded.

    The checks above exist for their targeted failure messages; this one is the
    guarantee. It is a total comparison — no keys are filtered, no normalization
    is applied — which is only viable because the schema is route/model-derived
    and provably deterministic: it depends on no runtime setting, and
    ``dump_openapi.py`` asserts its own YAML round-trip. Verified stable across
    repeated builds and a JSON round-trip.

    If this ever becomes flaky, do NOT relax it into a subset check without
    finding out WHY the schema became nondeterministic — that nondeterminism
    would itself be the bug.
    """
    spec = _load_committed_spec()
    generated = _generated_schema()

    if spec != generated:
        diffs = _first_diffs(spec, generated)
        pytest.fail(
            "agent/openapi/week2.yaml is OUT OF SYNC with the schema the app generates.\n"
            f"First {len(diffs)} difference(s):\n  " + "\n  ".join(diffs) + f"\nFix: {REGEN}."
        )
