"""feat_api criterion 4 — OpenAPI sync + contract tests: the normalized diff of
the app-generated schema vs the committed `agent/openapi/week2.yaml` is clean,
and the committed spec documents the Week-2 surface.

FROZEN GOALS. Comparison is structural (parsed YAML vs parsed JSON), so
formatting/key order never matters; any semantic drift fails with the first
differing paths named.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC_PATH = REPO_ROOT / "agent" / "openapi" / "week2.yaml"


def _first_diffs(a, b, path="$", out=None, limit=8):
    """Collect up to ``limit`` paths where two parsed documents differ."""
    if out is None:
        out = []
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append(f"{path}: {type(a).__name__} != {type(b).__name__}")
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in generated schema")
            elif k not in b:
                out.append(f"{path}.{k}: only in committed spec")
            else:
                _first_diffs(a[k], b[k], f"{path}.{k}", out, limit)
            if len(out) >= limit:
                break
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: list length {len(a)} != {len(b)}")
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                _first_diffs(x, y, f"{path}[{i}]", out, limit)
                if len(out) >= limit:
                    break
    elif a != b:
        out.append(f"{path}: {a!r} != {b!r}")
    return out


def test_api_04_openapi_spec_in_sync_with_app(client):
    try:
        import yaml
    except Exception:  # pragma: no cover - environment guard
        pytest.fail(
            "PyYAML is required to verify the committed OpenAPI spec — "
            "add pyyaml to the agent [dev] dependencies"
        )

    assert SPEC_PATH.is_file(), (
        "the OpenAPI 3 spec must be committed at agent/openapi/week2.yaml "
        "(W2_ARCHITECTURE.md 'Interfaces & contracts'); file is missing"
    )
    spec = yaml.safe_load(SPEC_PATH.read_text())
    assert isinstance(spec, dict), "week2.yaml must parse to an OpenAPI document object"

    live = client.get("/openapi.json")
    assert live.status_code == 200, f"GET /openapi.json -> {live.status_code}"
    generated = live.json()

    # The committed spec must actually document the Week-2 surface (guards a
    # trivially-synced spec for an app that never grew the document routes).
    paths = spec.get("paths") or {}
    assert "/v1/documents" in paths and "post" in paths["/v1/documents"], (
        f"week2.yaml must document POST /v1/documents; paths: {sorted(paths)}"
    )
    assert any("/pages/" in p for p in paths), (
        f"week2.yaml must document the page-image endpoint "
        f"(/v1/documents/{{id}}/pages/{{n}}); paths: {sorted(paths)}"
    )

    if spec != generated:
        diffs = _first_diffs(spec, generated)
        pytest.fail(
            "agent/openapi/week2.yaml is OUT OF SYNC with the app-generated "
            "schema (normalized structural diff must be clean). First diffs: "
            + "; ".join(diffs)
        )
