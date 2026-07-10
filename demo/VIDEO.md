# Demo Video — AgentForge Clinical Co-Pilot

**▶ Watch the walkthrough: https://www.loom.com/share/762e2fa76307493594f48862e8cccee5**

A recorded walkthrough of the **live** deployment (http://198.199.68.21/) showing the
Rounds Co-Pilot end to end:

- **Sickest-first triage** — the agent ranks the clinician's full patient census and
  opens on the most acute patient (Marcus Webb, DKA) instead of a dashboard.
- **Grounded, source-cited chart summary** — one row per metric with trends, and a
  provenance chip on every claim (no ungrounded text).
- **Grounded chat, fail-closed** — a SERVED answer that cites the record, and a WITHHELD
  refusal when asked about data that isn't there ("I can't confirm that from this
  patient's record") rather than a hallucination.
- **Proactive deterioration + physician-in-control** — a not-yet-seen patient (June
  Okafor, sepsis) is surfaced for a jump; the physician decides; the round advances by
  acuity, not room number.

See [`SCRIPT.md`](SCRIPT.md) for the shot list and [`../ACCESS.md`](../ACCESS.md) for how
to reach both the live and local environments.
