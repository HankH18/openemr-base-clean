# Week 3 — Care-Team Access Scoping (Break-Glass)

> **FORWARD PLANNING ONLY — we are still in WEEK 2 (final submission not yet made).** This is a
> roadmap note captured now for a week-3 reader. It is NOT current work and does not change any
> Week-2 deliverable.

**Status:** Planned for week 3. Gated on the user's care-team data work (see §"The one real
blocker"). The user plans to implement care teams and enrich assignment data in week 3, which is
exactly what unblocks this feature.

**Origin:** During the week-2 audit loop, "self-granted authorization" was flagged as a defect,
then RETRACTED after a source-cited reconciliation (see `.swarm-loop/overnight-log.md` →
"Broad-access-plus-audit — DECIDED"). The conclusion: broad-access-plus-audit is the correct,
intended DEFAULT (matches OpenEMR's own model + HIPAA treatment norms). This feature is
**least-privilege layered on top of that default**, not a fix to a hole.

---

## What it is

Keep broad access, but use OpenEMR's **native FHIR CareTeam** data to distinguish routine from
non-routine access, and record the distinction. Recommended shape: **advisory break-glass
marking** — never a hard block.

- At `rounds/start` (and/or at chat/document access time), resolve whether the acting clinician
  is on the target patient's care team.
- If YES → routine access, audit as today.
- If NO → still allow (broad access is the default), but STAMP the `audit_log` row as an
  outside-assignment / break-glass access, so it is reviewable.

This is the real-hospital pattern: broad access for treatment, with non-routine access flagged
for after-the-fact review rather than blocked up front. It degrades gracefully — a patient with
no care team simply yields no flag.

**Explicitly NOT recommended:** a hard per-patient 403. It fights clinical workflow, is
inconsistent with the physician's own OpenEMR `user/*` token (which already grants every patient
they can see), and breaks the demo on unpopulated care-team data. If a specific deployment needs
hard restriction (e.g. 42 CFR Part 2 substance-use records, behavioral health, VIP/employee
patients), it should be an opt-in mode on top of the marker, not the default.

---

## What is ALREADY built (discovery done — do not re-derive)

All verified in the week-2 codebase:

1. **The FHIR client is generic.** `copilot/fhir/client.py::search(resource_type, params)` already
   paginates. Reaching `GET /fhir/CareTeam?patient={uuid}` needs only adding `CareTeam` to the
   `ResourceType` enum (`copilot/domain/primitives.py`, ~line 44) — one line.

2. **The clinician → Practitioner identity mapping already exists.** This is the part that would
   normally be the hard subsystem, and it's done. `copilot/auth/identity.py` resolves the SMART
   `fhirUser` claim (which IS the physician's Practitioner reference) to the stable integer
   clinician_id at login. So matching the acting clinician against a care team's practitioner
   members is a comparison against an identity we already hold, not new plumbing.

3. **The audit row already carries nullable provenance columns.** `AuditLogRow`
   (`copilot/memory/models.py`) has nullable `entry_mode` / `source_ref`. Adding an
   `access_basis` (or similar) marker is a small nullable-column migration mirroring the existing
   `0009` pattern (`batch_alter_table` + nullable `add_column` — safe on a populated Postgres;
   prove the round trip FILE-BACKED, not against the `:memory:` default).

4. **The OpenEMR-native FHIR surface is confirmed present and patient-searchable:**
   - `GET /fhir/CareTeam?patient={uuid}` → assigned Practitioners + SNOMED roles
     (`src/Services/FHIR/FhirCareTeamService.php`).
   - `Patient.generalPractitioner` → primary provider (`FhirPatientService.php`), searchable via
     `?generalPractitioner=`.
   - Encounter and Appointment participants also carry provider↔patient links if a richer signal
     is wanted later.

---

## The one real blocker (this is the actual cost, and it's the user's week-3 data work)

**The demo OpenEMR instance has ZERO assignment data.** Verified on the deployed DB
(`care_teams`=0, `care_team_member`=0, patients with a non-null `providerID`=0 out of 15). Until
care teams are seeded, `CareTeam?patient=…` returns empty for every patient, so EVERY access
would be flagged break-glass and the marker would be pure noise, demonstrating nothing.

**This is precisely the week-3 work the user is planning** ("implement the care teams and enrich
the data as necessary"). Once care teams exist — the demo physician assigned to a SUBSET of the
patients — the marker becomes meaningful: assigned access is clean, unassigned access is flagged.
For a robust demo, the seed should be **reproducible** (folded into the co-pilot's demo seed
script so it survives a DB reset), not hand-entered once.

---

## The one operational step

The co-pilot's requested SMART scopes (`config.py::smart_scopes`) do NOT include
`user/CareTeam.read`. Adding it requires:
1. Confirm OpenEMR's FHIR CapabilityStatement actually lists CareTeam — requesting an unsupported
   scope makes BOTH client registration and the authorize call fail with `invalid_scope`
   (this is why `MedicationStatement` is deliberately omitted today — same failure mode).
2. Re-register the SMART app with the new scope and re-consent — a deploy-time operator step like
   the ones in `DEPLOY.md §16`, not code.

---

## Rough effort

- **Code:** ~half a day. `CareTeam` enum line; a `_care_team_practitioners(patient_uuid)` helper
  (reuses the generic `search`); compare against the `fhirUser` Practitioner ref already held;
  thread an `access_basis` flag into the audit row; the nullable migration; tests + sabotage proof.
- **Data seeding (the real cost):** folded into the user's planned care-team build. Make it
  reproducible in the demo seed.
- **Operational:** one SMART re-registration for `user/CareTeam.read`.

So: ~half a day of code that is INERT until assignment data exists, plus the care-team data build
(user-owned) that makes it meaningful, plus a one-time re-registration. The code is cheap; the
value comes entirely from the data.

---

## Acceptance criteria (for the week-3 build agent)

- A clinician ON the target patient's care team → access served, audit row marked routine.
- A clinician NOT on the care team → access STILL served (never a 403), audit row marked
  break-glass/outside-assignment.
- A patient with no care team → served, no false break-glass flag (degrades cleanly).
- The marker is queryable in the audit trail (an operator can list outside-assignment accesses).
- Demo seed is reproducible: after a DB reset + re-seed, some patients are assigned and some are
  not, so the demo shows both states.
- `user/CareTeam.read` is validated against OpenEMR's CapabilityStatement before it's requested.
- Broad access remains the DEFAULT — no path hard-blocks on assignment. (If a hard-restrict mode
  is added, it is opt-in and clearly separate.)
