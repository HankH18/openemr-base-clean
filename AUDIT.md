# OpenEMR Clinical Co-Pilot — Audit

Fork audited: `Gauntlet-HQ/openemr-base-clean` (OpenEMR 8.2 line).

This audit reads the platform the Clinical Co-Pilot must integrate into and translates what it
finds into the **requirements the product must satisfy** and the **constraints it must operate
under**. It is organized by the five audit dimensions the brief calls for; each closes with the
requirements and constraints that dimension imposes on the product.

> **Scope note.** The static dimensions (security, architecture, data quality, compliance) are
> established by reading the platform directly. **Performance under load** is stated as
> constraints and methodology only, and marked *pending* until the fork is deployed and
> load-tested; the numbers get filled in during MVP Stages 1–2.

---

## Summary (~500 words)

The most consequential finding is a data one: the platform's seed dataset is **demographics
only** — patient records with no clinical content behind them. A product whose entire value is
synthesizing "what changed for this patient" has nothing to synthesize against a stock install.
This makes the first requirement of the product non-negotiable and prior to everything else:
**it must generate a realistic clinical dataset** — encounters, results, medications, vitals,
notes, and at least one scripted change event — before it can function or be meaningfully
evaluated. Handled early, this becomes an asset: the same dataset serves as evaluation ground
truth, because the team authored it and knows the correct answers.

The security and integration picture is favorable and shapes the product's shape directly. The
platform provides **standards-based authorization** (OAuth2 with SMART-on-FHIR scopes),
including a system-to-system mode for automated clients. This lets the product delegate access
decisions to the platform rather than reinventing them: interactive use authenticates **as the
clinician** so the platform enforces what they may see, and any background processing runs as a
**minimally-scoped system identity** rather than a borrowed session. The resulting requirement
is a clean trust boundary — **no read path may bypass the platform's authorization** — and a
constraint to remember: a second, legacy authorization scheme also exists, so access reasoning
must account for both surfaces.

The data model imposes verification requirements. The same clinical fact can be represented in
more than one place and those representations can disagree; related clinical concepts share a
generic, loosely-typed structure. The product therefore **must define an authoritative source
per fact and reconcile or flag conflicts** rather than trusting a single lookup. Working in the
product's favor, the platform models **data provenance as a first-class capability** and its
results data carries **reference ranges and abnormal indicators** — so both grounding ("this
claim traces to that record") and domain-safety checks ("this value is out of range") can be
built on real source data rather than invented.

Compliance is a constraint that touches every layer. All patient data and any artifact derived
from it is PHI, so the product **must encrypt at rest and in transit, keep PHI out of logs,
maintain its own access audit trail, operate under a Business Associate Agreement assumption,
use demo data only, and define retention** for conversation history and derived artifacts.

Finally, a change-awareness constraint: the platform does **not push change events**, but it
does support cheap "did anything change" queries. So the product's freshness must be **poll-
based for v1** (event-driven is a later scale path), and it **must gate expensive model work
behind cheap change detection** so cost and latency scale with the *rate of change*, not with
the number of patients times the polling frequency. Performance under load is not yet measured;
the dominant expected constraint is that standardized reads are per-resource, so assembling a
full patient view takes several calls — mitigated by only re-reading patients that changed.

---

## 1. Security & access control

**Findings (general).** The platform offers standards-based authorization with both a
user-delegated flow and a system-to-system flow for automated clients. Clinical data is
returned by APIs that are only as safe as the token issued to reach them. A separate,
older authorization scheme governs the platform's own web interface. Introducing a language
model adds a new exposure: free-text clinical fields become a potential injection vector once
something reads and acts on them.

**Requirements & constraints.**
- The product **must** authenticate as the requesting clinician for interactive use and let the
  platform make the access decision.
- The product **must** run all automated/background access under a **least-privilege system
  identity**, never a borrowed user session, and confine it to the minimum data it needs.
- The product **must** enforce a serve-time authorization check so that broad background reads
  never become broad disclosure — a clinician only ever receives data for patients they are
  authorized to see.
- The product **must** treat retrieved clinical text as *data, never instructions*, and keep
  trust decisions in deterministic code outside the model.
- **Constraint:** two authorization schemes coexist; any "who can see what" reasoning must
  account for both.

## 2. Architecture & integration

**Findings (general).** The platform is a large, long-lived monolith with a mix of modern and
legacy code. It exposes multiple layered ways to read the same data — a standardized clinical
API, a proprietary API, and an internal service layer — and provides clean extension and
eventing points. Direct access to the underlying schema is possible but hazardous given its
size and the mix of conventions.

**Requirements & constraints.**
- The product **must** integrate through the platform's data-access layers, not by querying the
  underlying schema directly.
- The product **should** be a **separately deployable service**, to avoid coupling to the
  monolith's release cycle and to keep the agent's toolchain and language independent of it.
- **Opportunity:** the data-access layer is effectively swappable behind an integration
  boundary, so the product can change how it reads without changing what it does.
- **Constraint:** a full patient view spans several data types, implying multiple reads per
  patient — a factor the update and latency design must plan around.

## 3. Data quality  *(highest-impact dimension)*

**Findings (general).** The seed dataset contains patient demographics only — no clinical
content. The same clinical concept can be stored in more than one place and the copies can
disagree. Several related clinical concepts share a single, generically-typed structure that is
easy to mis-interpret. On the positive side, results data carries reference ranges and abnormal
indicators.

**Requirements & constraints.**
- **Requirement (Stage 0):** the product **must generate a realistic clinical dataset** before
  any agent work — and reuse it as evaluation ground truth.
- **Requirement:** where a clinical fact has multiple representations, the product **must**
  define an authoritative source and **reconcile or flag** disagreements rather than silently
  picking one.
- **Requirement:** the product **must** read clinical concepts through typed, well-understood
  interfaces rather than the generic underlying structures, to avoid mis-classification.
- **Opportunity/constraint:** because results carry reference ranges and abnormal flags, the
  product's domain-safety checks can be **grounded in real source data**, not heuristics.

## 4. Compliance & regulatory (HIPAA)

**Findings (general).** All patient data — and any artifact derived from it, including
summaries and conversation history — is Protected Health Information. Access must be logged;
sending PHI to a model provider implies a Business Associate Agreement; retention and
audit-immutability are live concerns.

**Requirements & constraints.**
- The product **must** encrypt PHI at rest and in transit and **must never** write PHI to
  application logs (identifiers and a correlation ID only).
- The product **must** maintain its **own access audit trail** — who accessed which patient,
  when, and what was returned.
- The product **must** operate under a **BAA assumption with a no-training guarantee** and use
  **demo data only**.
- The product **must** define a **retention policy** for conversation history and derived
  artifacts, and keep audit records **append-only**. Design the datastore assuming a retention
  rule will land on it.
- **Constraint to watch:** if the product's output is ever treated as clinical *decision
  support* rather than information retrieval, additional obligations (human sign-off, certified
  decision-support pathways) attach — a material change to the compliance surface.

## 5. Performance & scale  *(pending deployment — constraints + methodology)*

**Findings (general).** Not yet measured under load. Standardized reads are per-resource, so a
full patient view requires several calls. The platform does not push change events, but it does
support cheap "did anything change" queries. Model-synthesis time dominates the background path;
interactive responses are the latency-sensitive path.

**Requirements & constraints.**
- **Constraint:** change awareness **must be poll-based** for v1; event-driven change delivery
  is a future scale path, not a v1 capability.
- **Requirement:** the product **must** gate expensive model work behind **cheap change
  detection** so cost and latency scale with the **rate of change**, not patients × frequency.
- **Requirement:** interactive responses **must** be tuned for low latency; background synthesis
  **may** be asynchronous, and the product **must** always communicate data freshness so a
  clinician never unknowingly relies on a stale picture.
- **Pending measurement:** baseline CPU/memory/throughput and p50/p95/p99 latency and error rate
  at 10 and 50 concurrent users, captured once deployed.

---

## Most important finding & its impact on the product

The demographics-only seed data. It reframes the product's first requirement — generate a
realistic clinical dataset — as prior to everything else, and turns a would-be showstopper into
an asset by reusing that dataset as evaluation ground truth. The favorable security and data
findings then let the product **lean on platform primitives** (standards-based authorization,
first-class provenance, reference-range/abnormal data) instead of reinventing access control,
grounding, and safety checks — which is what keeps the architecture defensible under scrutiny.

## Consolidated requirements & constraints

1. Generate realistic clinical data first; reuse it as eval ground truth. *(requirement)*
2. Integrate through the platform's data-access layers; run as a separate service. *(requirement)*
3. Delegate interactive access to the platform (as the clinician); run background access as a
   least-privilege system identity; re-authorize at serve time. *(requirement)*
4. Treat clinical text as data, never instructions; keep trust decisions in deterministic code.
   *(requirement)*
5. Define an authoritative source per fact; reconcile or flag conflicts. *(requirement)*
6. Encrypt PHI, keep it out of logs, keep an access audit trail, assume a BAA, use demo data,
   define retention, keep audit append-only. *(requirement)*
7. Poll for change (no push); gate model work behind cheap change detection. *(constraint)*
8. Tune interactive latency; allow async background synthesis; always show freshness.
   *(requirement)*
9. Account for two authorization schemes; watch for a decision-support reclassification.
   *(constraint)*
