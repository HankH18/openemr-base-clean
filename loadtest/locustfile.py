"""Locust load test for the AgentForge Clinical Co-Pilot agent.

Exercises the four required surfaces — ``/v1/chat``, ``/v1/rounds/*``,
``/health``, ``/ready`` — at 10 and 50 concurrent users.

This is the preferred/canonical load script. Where Locust is installed it is
the tool of record; in environments where Locust's ``gevent`` dependency will
not build (e.g. a restricted CI sandbox), ``smoke_load.py`` in this directory is
a dependency-light stand-in that produces the same p50/p95/p99 + error-rate
report using only ``httpx`` (see RESULTS.md for which harness produced the
captured numbers).

**Auth mode — this harness targets a ``disabled``-mode instance.** The data
routes (``/v1/chat``, ``/v1/rounds/*``) resolve identity from ``auth_mode``. In
``disabled`` mode (the default; what ``run.sh`` boots) the acting clinician comes
from the request ``clinician_id`` sent below, so these unauthenticated calls
work. On a ``smart``-mode deployment (e.g. the live droplet) the SAME routes
return **401** without a valid ``af_session`` session cookie — so this naive
unauthenticated driver cannot load-test them there. Authenticated load testing
against smart mode requires first establishing a physician session (a seeded
``af_session`` cookie) and replaying it on every request; that is out of scope
for this offline harness. Run this against a ``disabled``-mode instance
(``COPILOT_AUTH_MODE=disabled``, set explicitly in ``run.sh``).

Run (headless), 10 then 50 users:

    # boot the agent first (see run.sh), then:
    locust -f loadtest/locustfile.py --headless \
        -u 10 -r 5 -t 60s --host http://localhost:8010 \
        --csv loadtest/results_10u

    locust -f loadtest/locustfile.py --headless \
        -u 50 -r 10 -t 60s --host http://localhost:8010 \
        --csv loadtest/results_50u

The ``--csv`` output includes ``*_stats.csv`` with p50/p95/p99 columns.

NOTE: this file lives under ``loadtest/`` (outside the agent's pytest
``testpaths`` of tests/ and evals/) and is named ``locustfile.py`` — it is never
collected or imported by the agent test suite.
"""

from __future__ import annotations

import random

from locust import HttpUser, between, task

# A clinician who has established a round (so chat is authorized) and a patient
# panel to walk. Matches loadtest/seed_data.py.
CLINICIAN_ID = 1
PATIENT_IDS = [101, 102, 103]

CHAT_QUESTIONS = [
    "What is the patient's most recent potassium?",
    "Summarize what changed since yesterday.",
    "Are there any critical labs?",
    "What medications is the patient on?",
    "Any allergy conflicts I should know about?",
]


class ClinicianUser(HttpUser):
    """Simulates a hospitalist driving the co-pilot during rounds."""

    # Think time between actions — clinicians read the card before the next tap.
    wait_time = between(0.5, 2.0)

    @task(1)
    def health(self) -> None:
        self.client.get("/health", name="GET /health")

    @task(1)
    def ready(self) -> None:
        # 503 is an expected (not-error) response when a dependency is down;
        # mark only 5xx>=500 other than 503 as a failure.
        with self.client.get("/ready", name="GET /ready", catch_response=True) as resp:
            if resp.status_code in (200, 503):
                resp.success()

    @task(4)
    def rounds_current(self) -> None:
        self.client.get(
            f"/v1/rounds/current?clinician_id={CLINICIAN_ID}",
            name="GET /v1/rounds/current",
        )

    @task(2)
    def rounds_start(self) -> None:
        self.client.post(
            "/v1/rounds/start",
            json={"clinician_id": CLINICIAN_ID, "patient_ids": PATIENT_IDS},
            name="POST /v1/rounds/start",
        )

    @task(1)
    def rounds_advance(self) -> None:
        self.client.post(
            "/v1/rounds/advance",
            json={"clinician_id": CLINICIAN_ID, "completed_patient_id": random.choice(PATIENT_IDS)},
            name="POST /v1/rounds/advance",
        )

    @task(3)
    def chat(self) -> None:
        self.client.post(
            "/v1/chat",
            json={
                "clinician_id": CLINICIAN_ID,
                "patient_id": random.choice(PATIENT_IDS),
                "message": random.choice(CHAT_QUESTIONS),
            },
            name="POST /v1/chat",
        )
