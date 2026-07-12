#!/usr/bin/env bash
# Reproducible load-test run for the AgentForge Clinical Co-Pilot agent.
#
# Boots the agent locally against a seeded throwaway SQLite DB, then drives it
# at 10 and 50 concurrent users, capturing p50/p95/p99 + error rate per
# endpoint. Prefers Locust (locustfile.py); falls back to the httpx driver
# (smoke_load.py) when Locust's gevent dependency can't build.
#
# Usage:  bash loadtest/run.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$REPO_ROOT/agent"
VENV_PY="$AGENT_DIR/.venv/bin/python"
PORT=8010
HOST="http://127.0.0.1:$PORT"
DB="/tmp/copilot_loadtest.db"

export COPILOT_DATABASE_URL="sqlite+aiosqlite:///$DB"
# The copilot package isn't pip-installed in the venv; put agent/ on the path so
# the seed script (run by path) and uvicorn can import it.
export PYTHONPATH="$AGENT_DIR${PYTHONPATH:+:$PYTHONPATH}"
# Keep the run offline & fast: no LLM key (chat uses the deterministic StubAgent
# and returns 200 fail-closed), no poller. Dependency probes fail fast.
unset COPILOT_ANTHROPIC_API_KEY ANTHROPIC_API_KEY || true
export COPILOT_POLLER_ENABLED=false
# Load-test configuration: DISABLED auth mode. The data routes (/v1/chat,
# /v1/rounds/*) then take the acting clinician from the request clinician_id the
# drivers send, so unauthenticated load works. A smart-mode instance would 401
# these routes without an af_session cookie (see locustfile.py / RESULTS.md);
# authenticated smart-mode load testing needs a seeded session and is out of
# scope for this offline harness. Write-back stays OFF (default) — not exercised.
export COPILOT_AUTH_MODE=disabled

rm -f "$DB"

echo ">> seeding DB at $DB"
( cd "$AGENT_DIR" && "$VENV_PY" "$REPO_ROOT/loadtest/seed_data.py" )

echo ">> booting agent on $HOST"
( cd "$AGENT_DIR" && "$VENV_PY" -m uvicorn copilot.api.app:app --port "$PORT" --log-level warning ) &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null || true' EXIT

# wait for /health
for _ in $(seq 1 30); do
  if curl -sf "$HOST/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

if command -v locust >/dev/null 2>&1; then
  echo ">> Locust available — running canonical harness"
  locust -f "$REPO_ROOT/loadtest/locustfile.py" --headless -u 10 -r 5 -t 30s \
    --host "$HOST" --csv "$REPO_ROOT/loadtest/results_10u"
  locust -f "$REPO_ROOT/loadtest/locustfile.py" --headless -u 50 -r 10 -t 30s \
    --host "$HOST" --csv "$REPO_ROOT/loadtest/results_50u"
else
  echo ">> Locust not installed — using httpx smoke_load.py fallback"
  "$VENV_PY" "$REPO_ROOT/loadtest/smoke_load.py" --host "$HOST" --users 10 --duration 20 \
    --out "$REPO_ROOT/loadtest/results_10u.json"
  "$VENV_PY" "$REPO_ROOT/loadtest/smoke_load.py" --host "$HOST" --users 50 --duration 20 \
    --out "$REPO_ROOT/loadtest/results_50u.json"
fi

echo ">> done. See loadtest/RESULTS.md and the results_* files."
