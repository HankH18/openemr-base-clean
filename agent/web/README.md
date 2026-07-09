# Rounds — Clinical Co-Pilot (web)

React front-end for the AgentForge Clinical Co-Pilot. A hospitalist opens on
their most acute patient, reads a grounded chart summary plus what changed
overnight, drills down by asking questions, and advances patient by patient.
Every clinical claim is traceable to a source record; unverifiable claims are
withheld, never guessed.

## Run it

```bash
npm install
npm run dev        # http://localhost:5173 — full experience on the mock cohort
```

Other scripts:

```bash
npm run typecheck  # tsc --noEmit (strict)
npm run build      # typecheck + vite production build → dist/
npm run preview    # serve the production build
```

No backend is required: with no configuration the app uses a **mock adapter**
seeded with a five-patient cohort (NSTEMI, sepsis, hyperkalemia, a
drug–allergy conflict, one stable patient).

## Pointing at the real API

Set `VITE_API_BASE_URL` to the Co-Pilot FastAPI service and the typed HTTP
adapter replaces the mock:

```bash
VITE_API_BASE_URL=http://localhost:8000 npm run dev
# or put it in .env.local:  VITE_API_BASE_URL=http://localhost:8000
```

The seam is `src/api/client.ts` (`createApi()`); both adapters implement the
same `CopilotApi` interface. Notes on the live adapter:

- `patient_id` values are normalized (`{value: n}` or bare `n`) in
  `src/api/normalize.ts`; card payloads may arrive bare or wrapped in
  `{current, order}`.
- The service has no dedicated "jump" endpoint, so accepting a deterioration
  alert re-issues `POST /v1/rounds/start` over the not-yet-seen patient ids.
  The server re-ranks by acuity, which puts the alerted patient on top.
- Patient display identity (name, bed, service line) is a UI-side census
  roster in `src/census.ts` — the API deals in ids only. Swap the roster for
  a live census feed without touching the rounds/chat plumbing.
- Unknown verification actions from the service are rendered as `degraded`
  (never upgraded to a normal answer).

## Demo script (mock cohort)

1. The round opens on **Ernest Vaughn (1001)** — critical troponin, acuity
   9.1. Each claim carries a provenance chip (`Observation · trop-1001`);
   press one to see the exact resource/field/value it cites.
2. Ask the chart: **"Latest troponin?"** → a served, verified answer with
   citations. **"What did the ECG show?"** → a *degraded* answer (source
   re-check incomplete). **"Any MRI report?"** → a *withheld* answer — the
   record has no source, so the Co-Pilot says so instead of guessing.
3. ~12 seconds after the round starts (or immediately after pressing
   **Re-check charts**), **Lillian Cho (1005)** deteriorates: a critical
   lactate lands, her acuity jumps 4.2 → 9.3, and a non-modal banner offers
   to jump to her. Accept or dismiss — the physician decides.
4. **Done — next patient** marks the current patient seen and advances by
   acuity. **June Okafor (1004)** demonstrates the stale-card treatment;
   **Marcus Webb (1003)** carries a penicillin/amoxicillin conflict rendered
   as a critical safety strip despite his low acuity.

## Structure

```
src/
  api/
    types.ts       Wire types + ApiError (mirrors copilot/domain/contracts.py)
    normalize.ts   Shape normalizers ({value}|number ids, wrapped cards, …)
    client.ts      CopilotApi interface + createApi() adapter switch
    http.ts        Live adapter (fetch, VITE_API_BASE_URL)
    mock.ts        Mock adapter: session state, latency, scripted deterioration
    cohort.ts      Seeded cohort content (claims with verbatim source values)
  state/
    useRounds.ts   Round session state machine (start/advance/jump/re-check)
    useAlerts.ts   5 s deterioration polling; render-time filtering
    useChat.ts     Per-patient threads, pending/verification states
    theme.ts       Light/dark toggle (data-theme wins over the media query)
  components/      TopBar, QueueRail, PatientHero, AcuityMeter, FreshnessTag,
                   ClaimList, ProvenanceChip, ChatPanel, AlertBanner, CompleteView
  styles/          tokens.css (both themes), base.css, app.css
  census.ts        UI-side patient roster + clinician identity
  suggestions.ts   Starter questions per patient
```

## Design notes

Clinical-ledger aesthetic: cool chart-paper neutrals, blue-black ink, petrol
brand accent kept strictly separate from the clinical semantic hues
(critical red / caution amber / verified green — green appears only on
verification outcomes). Type: Newsreader (display), Schibsted Grotesk (body),
Spline Sans Mono for all data with `tabular-nums`. Fonts ship via
`@fontsource-variable/*` — no CDN. Motion is deliberate and respects
`prefers-reduced-motion`.
