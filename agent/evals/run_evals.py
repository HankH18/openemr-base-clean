#!/usr/bin/env python3
"""Deterministic eval runner for the Clinical Co-Pilot.

Executes ``eval_dataset.jsonl`` against a real, in-process instance of the
FastAPI app — the SAME black-box HTTP contract the frozen acceptance suite
uses — but with NO Anthropic key, so the app takes its deterministic
stub-agent path. Every case therefore has one, and only one, correct outcome:
the run needs no LLM and no network.

The harness mirrors ``.swarm-loop/acceptance/conftest.py`` (a temp-file SQLite
DB, a respx-faked OpenEMR, ``create_app`` + ``TestClient``) but is fully
self-contained — nothing is imported from the frozen ``.swarm-loop`` tree.

Cases cover the four boundary/invariant/authorization behaviors the eval doc
calls for:

- **served** — a grounded question about present data (invariant).
- **withheld** — an ungroundable question, and a value that drifted vs the
  live record (boundary / fail-closed).
- **refused** — a chat about a patient outside the clinician's rounding list,
  and a clinician with no session at all (authorization).
- **no-leak / ranking** — cross-patient isolation and sickest-first ranking.

Run it (from ``agent/``)::

    ./.venv/bin/python evals/run_evals.py

It prints a per-case table + a ``passed/total`` (accuracy %) summary, and writes
two committed-in-tree artifacts next to the dataset: ``eval_results.json`` (the
machine-readable record) and ``EVAL_RESULTS.md`` (the readable report).

@package   OpenEMR
@link      https://www.open-emr.org
@license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# Make the `copilot` package and the `evals` package importable no matter how
# the runner was invoked: a bare `python evals/run_evals.py` puts the script's
# own dir (evals/) on sys.path, not agent/. Mirror the acceptance conftest.
_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

EVALS_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVALS_DIR / "eval_dataset.jsonl"
RESULTS_JSON_PATH = EVALS_DIR / "eval_results.json"
RESULTS_MD_PATH = EVALS_DIR / "EVAL_RESULTS.md"

# Fixed "captured at" stamp so the committed artifacts are byte-stable across
# re-runs (the fixtures are frozen, so the outcome never changes).
CAPTURED_AT = "2026-07-10T00:00:00Z"


def _configure_env(db_file: Path) -> None:
    """Point Settings at a temp SQLite file + the fake OpenEMR; NO LLM key.

    Identical intent to the acceptance conftest's ``_env`` fixture: an empty
    ``COPILOT_ANTHROPIC_API_KEY`` selects the deterministic stub agent, and the
    empty Langfuse keys select the no-op observability backend.
    """
    from evals._fake_openemr import (
        FHIR_BASE_URL,
        OAUTH_AUTHORIZE_URL,
        OAUTH_TOKEN_URL,
    )

    os.environ["COPILOT_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file}"
    os.environ["COPILOT_FHIR_BASE_URL"] = FHIR_BASE_URL
    os.environ["COPILOT_OAUTH_TOKEN_URL"] = OAUTH_TOKEN_URL
    os.environ["COPILOT_OAUTH_AUTHORIZE_URL"] = OAUTH_AUTHORIZE_URL
    os.environ["COPILOT_SMART_APP_CLIENT_ID"] = "eval-smart"
    os.environ["COPILOT_BACKEND_SERVICES_CLIENT_ID"] = "eval-backend"
    os.environ["COPILOT_ANTHROPIC_API_KEY"] = ""  # -> deterministic stub agent
    os.environ["COPILOT_LANGFUSE_HOST"] = ""
    os.environ["COPILOT_LANGFUSE_PUBLIC_KEY"] = ""
    os.environ["COPILOT_LANGFUSE_SECRET_KEY"] = ""


def _clear_caches() -> None:
    from copilot.config import get_settings
    from copilot.memory.db import get_engine, get_session_factory

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _create_schema(db_file: Path) -> None:
    """Create every table on the temp DB via a loop-agnostic SYNC engine."""
    import sqlalchemy as sa

    import copilot.memory.models  # noqa: F401  (registers tables on Base.metadata)
    from copilot.memory.db import Base

    sync_engine = sa.create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()


def _make_client() -> Any:
    from fastapi.testclient import TestClient

    from copilot.api.app import create_app
    from copilot.config import get_settings

    # probe_factories=[] -> /ready is trivially ready; chat/rounds need no probes.
    return TestClient(create_app(get_settings(), probe_factories=[]))


def _load_dataset() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line in DATASET_PATH.read_text().splitlines():
        line = line.strip()
        if line:
            cases.append(json.loads(line))
    return cases


def _pid(value: Any) -> int:
    """PatientId may serialise as int or {"value": int} — accept either."""
    if isinstance(value, dict):
        return int(value["value"])
    return int(value)


def _card(body: dict[str, Any]) -> dict[str, Any]:
    inner = body.get("current") if isinstance(body, dict) else None
    return inner if isinstance(inner, dict) else body


# --- expectation checks ----------------------------------------------------


def _check_status(expect: dict[str, Any], status: int) -> list[str]:
    want = expect.get("status", 200)
    if status != want:
        return [f"status: expected {want}, got {status}"]
    return []


def _check_chat_body(expect: dict[str, Any], body: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    claims = body.get("claims") or []
    action = (body.get("verification") or {}).get("action")

    want_action = expect.get("verification_action")
    if want_action is not None and action != want_action:
        failures.append(f"verification.action: expected {want_action!r}, got {action!r}")

    min_claims = expect.get("min_claims")
    if min_claims is not None and len(claims) < min_claims:
        failures.append(f"claims: expected >= {min_claims}, got {len(claims)}")

    max_claims = expect.get("max_claims")
    if max_claims is not None and len(claims) > max_claims:
        failures.append(f"claims: expected <= {max_claims}, got {len(claims)}")

    cited_value = expect.get("cited_value")
    if cited_value is not None:
        values = {c["source_ref"]["value"] for c in claims}
        if cited_value not in values:
            failures.append(f"cited_value: expected {cited_value!r} among {sorted(values)}")

    forbidden = expect.get("forbidden_resource_ids")
    if forbidden is not None:
        cited_ids = {c["source_ref"]["resource_id"] for c in claims}
        leaked = cited_ids & set(forbidden)
        if leaked:
            failures.append(f"forbidden_resource_ids leaked: {sorted(leaked)}")

    if expect.get("answer_nonempty") and not (body.get("answer") or "").strip():
        failures.append("answer_nonempty: expected a non-empty honest answer")

    return failures


def _check_rounds_body(expect: dict[str, Any], body: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    card = _card(body)

    want_top = expect.get("top_patient_id")
    if want_top is not None:
        got_top = _pid(card.get("patient_id"))
        if got_top != want_top:
            failures.append(f"top_patient_id: expected {want_top}, got {got_top}")

    if expect.get("rank_reason_nonempty") and not str(card.get("rank_reason", "")).strip():
        failures.append("rank_reason_nonempty: expected a non-empty rank_reason")

    return failures


# --- case execution --------------------------------------------------------


def _start_round(client: Any, clinician_id: int, patient_ids: list[int]) -> Any:
    return client.post(
        "/v1/rounds/start",
        json={"clinician_id": clinician_id, "patient_ids": patient_ids},
    )


def _run_chat_case(client: Any, case: dict[str, Any]) -> dict[str, Any]:
    expect = case["expect"]
    session_ids = case.get("session_patient_ids")
    if session_ids:
        start = _start_round(client, case["clinician_id"], session_ids)
        if start.status_code != 200:
            return _fail(case, [f"round setup failed: /v1/rounds/start -> {start.status_code}"])

    resp = client.post(
        "/v1/chat",
        json={
            "clinician_id": case["clinician_id"],
            "patient_id": case["patient_id"],
            "message": case["message"],
        },
    )
    failures = _check_status(expect, resp.status_code)
    detail: dict[str, Any] = {"status": resp.status_code}
    if not failures and resp.status_code == 200:
        body = resp.json()
        failures += _check_chat_body(expect, body)
        detail["action"] = (body.get("verification") or {}).get("action")
        detail["claims"] = len(body.get("claims") or [])
    return _result(case, failures, detail)


def _run_rounds_case(client: Any, case: dict[str, Any]) -> dict[str, Any]:
    expect = case["expect"]
    resp = _start_round(client, case["clinician_id"], case["session_patient_ids"])
    failures = _check_status(expect, resp.status_code)
    detail: dict[str, Any] = {"status": resp.status_code}
    if not failures and resp.status_code == 200:
        body = resp.json()
        failures += _check_rounds_body(expect, body)
        detail["top_patient_id"] = _pid(_card(body).get("patient_id"))
    return _result(case, failures, detail)


def _run_case(client: Any, case: dict[str, Any]) -> dict[str, Any]:
    kind = case["kind"]
    if kind == "chat":
        return _run_chat_case(client, case)
    if kind == "rounds":
        return _run_rounds_case(client, case)
    return _fail(case, [f"unknown case kind {kind!r}"])


def _result(case: dict[str, Any], failures: list[str], detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "category": case["category"],
        "kind": case["kind"],
        "description": case["description"],
        "passed": not failures,
        "failures": failures,
        "detail": detail,
    }


def _fail(case: dict[str, Any], failures: list[str]) -> dict[str, Any]:
    return _result(case, failures, {})


# --- artifacts + summary ---------------------------------------------------


def _summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    accuracy = round(100.0 * passed / total, 2) if total else 0.0
    by_cat: dict[str, dict[str, int]] = {}
    for r in results:
        bucket = by_cat.setdefault(r["category"], {"passed": 0, "total": 0})
        bucket["total"] += 1
        if r["passed"]:
            bucket["passed"] += 1
    return {
        "captured_at": CAPTURED_AT,
        "dataset": DATASET_PATH.name,
        "deterministic": True,
        "requires_api_key": False,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy_pct": accuracy,
        "by_category": by_cat,
    }


def _write_json(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    payload = {"summary": summary, "cases": results}
    RESULTS_JSON_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_markdown(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    lines: list[str] = []
    lines.append("# Clinical Co-Pilot — Eval Results")
    lines.append("")
    lines.append(
        f"Deterministic eval run (no ANTHROPIC key required — stub-agent path). "
        f"Captured at `{summary['captured_at']}`."
    )
    lines.append("")
    lines.append(
        f"**Score: {summary['passed']}/{summary['total']} passed "
        f"({summary['accuracy_pct']}% accuracy).**"
    )
    lines.append("")
    lines.append("## By category")
    lines.append("")
    lines.append("| Category | Passed | Total |")
    lines.append("| --- | --- | --- |")
    for cat in sorted(summary["by_category"]):
        bucket = summary["by_category"][cat]
        lines.append(f"| {cat} | {bucket['passed']} | {bucket['total']} |")
    lines.append("")
    lines.append("## Per-case outcomes")
    lines.append("")
    lines.append("| Case | Category | Result | Detail |")
    lines.append("| --- | --- | --- | --- |")
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        detail = "; ".join(r["failures"]) if r["failures"] else _fmt_detail(r["detail"])
        lines.append(f"| `{r['id']}` | {r['category']} | {mark} | {detail} |")
    lines.append("")
    lines.append("## What each behavior proves")
    lines.append("")
    lines.append("- **served** — a grounded question about present data returns cited claims.")
    lines.append("- **withheld** — an ungroundable question, and a value that drifted vs the "
                 "live record, both fail closed rather than guess.")
    lines.append("- **refused (403)** — chat about a patient outside the clinician's rounding "
                 "list, and a clinician with no session, are denied.")
    lines.append("- **no-leak / ranking** — cross-patient isolation and sickest-first ranking.")
    lines.append("")
    RESULTS_MD_PATH.write_text("\n".join(lines))


def _fmt_detail(detail: dict[str, Any]) -> str:
    if not detail:
        return ""
    return ", ".join(f"{k}={v}" for k, v in detail.items())


def _print_summary(summary: dict[str, Any], results: list[dict[str, Any]]) -> None:
    print("\nClinical Co-Pilot — deterministic eval run")
    print("=" * 60)
    for r in results:
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"  [{mark}] {r['id']} ({r['category']})")
        for f in r["failures"]:
            print(f"         - {f}")
    print("=" * 60)
    print(
        f"  {summary['passed']}/{summary['total']} passed "
        f"({summary['accuracy_pct']}% accuracy)"
    )
    print(f"  artifacts: {RESULTS_JSON_PATH.name}, {RESULTS_MD_PATH.name}")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_file = Path(tmp) / "evals.db"
        _configure_env(db_file)
        _clear_caches()
        _create_schema(db_file)

        from evals._fake_openemr import build_router

        cases = _load_dataset()
        with build_router():
            client = _make_client()
            results = [_run_case(client, case) for case in cases]

        _clear_caches()

    summary = _summarize(results)
    _write_json(summary, results)
    _write_markdown(summary, results)
    _print_summary(summary, results)
    # Non-zero exit if any case regressed, so CI can gate on it.
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
