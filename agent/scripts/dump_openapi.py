#!/usr/bin/env python3
"""Regenerate agent/openapi/week2.yaml from the app-generated OpenAPI schema.

Keeps the committed OpenAPI 3 spec structurally in sync with what the running
app serves at ``/openapi.json`` (the feat_api contract-sync check compares the
parsed YAML to the live schema). Run after ANY route or response-model change::

    python scripts/dump_openapi.py

The schema is route/model-derived and independent of runtime settings, so the
committed spec is deterministic across environments on a given FastAPI version.
"""

from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))


def main() -> None:
    import yaml

    from copilot.api.app import create_app
    from copilot.config import get_settings

    # probe_factories=[] matches the acceptance client build; probes are runtime
    # only and do not affect the generated schema.
    app = create_app(get_settings(), probe_factories=[])
    schema = app.openapi()

    out = _AGENT_DIR / "openapi" / "week2.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(schema, sort_keys=True, allow_unicode=True))

    # Guard against YAML type drift: the round-trip must reproduce the schema
    # exactly (this is what the contract-sync check asserts).
    reloaded = yaml.safe_load(out.read_text())
    if reloaded != schema:
        raise SystemExit("YAML round-trip diverged from the generated OpenAPI schema")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
