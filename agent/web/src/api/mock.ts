/**
 * Mock adapter — the default when VITE_API_BASE_URL is unset.
 *
 * Serves the seeded cohort with realistic latency and one scripted event:
 * ~12 seconds after the round starts (or immediately on a manual
 * "re-check charts"), patient 1005's critical lactate lands. Her card
 * regenerates at acuity 9.3 and she appears in /alerts until seen.
 */

import type { CopilotApi } from './client';
import { ALERT_REASON_1005, COHORT, type ChatFact, type CohortPatient, type CohortPhase } from './cohort';
import { patientName } from '../census';
import { newCorrelationId } from '../ids';
import { WRITABLE_METRICS, type WritableMetric } from '../labels';
import {
  ApiError,
  WriteRejectedError,
  type ChatRequest,
  type ChatResponse,
  type Claim,
  type CommittedWrite,
  type ConversationMessage,
  type DeteriorationAlert,
  type DocumentAccepted,
  type PatientCard,
  type ProposedWrite,
  type RefreshOutcome,
  type RoundView,
  type WriteCandidate,
} from './types';

const DETERIORATION_AFTER_MS = 12_000;
const DETERIORATING_PATIENT = 1005;

const cohortById = new Map<number, CohortPatient>(COHORT.map((p) => [p.id, p]));

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

function jitter(base: number, spread: number): number {
  return base + Math.random() * spread;
}

function clock(date: Date): string {
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${hh}:${mm}`;
}

interface Session {
  order: number[];
  seen: Set<number>;
  current: number;
  startedAt: number;
}

export function createMockApi(): CopilotApi {
  let session: Session | null = null;
  let deteriorationFired = false;
  /** Per-patient wall-clock of the last manual re-check (freshness reset). */
  const recheckedAt = new Map<number, number>();
  const conversations = new Map<number, ConversationMessage[]>();
  let nextConversationId = 9001;
  /** idempotency_key → the record already committed for it (double-confirm safe). */
  const committedWrites = new Map<string, CommittedWrite>();

  function maybeFireDeterioration(): void {
    if (deteriorationFired || session === null) {
      return;
    }
    if (Date.now() - session.startedAt >= DETERIORATION_AFTER_MS) {
      deteriorationFired = true;
    }
  }

  function phaseFor(patient: CohortPatient): CohortPhase {
    if (deteriorationFired && patient.deteriorated) {
      return patient.deteriorated;
    }
    return patient.base;
  }

  function requirePatient(patientId: number): CohortPatient {
    const patient = cohortById.get(patientId);
    if (!patient) {
      throw new ApiError(`Patient ${patientId} is not on the rounding list`, 404);
    }
    return patient;
  }

  function buildCard(patientId: number): PatientCard {
    const patient = requirePatient(patientId);
    const phase = phaseFor(patient);
    const rechecked = recheckedAt.get(patientId);
    const ageSeconds =
      rechecked !== undefined
        ? Math.max(0, Math.round((Date.now() - rechecked) / 1000))
        : phase.ageSeconds;
    const stale = rechecked !== undefined ? false : phase.stale;
    return {
      patient_id: patientId,
      summary_claims: phase.summary,
      changes_since_last_seen: phase.changes,
      acuity_score: phase.acuity,
      rank_reason: phase.rankReason,
      freshness: {
        as_of: new Date(Date.now() - ageSeconds * 1000).toISOString(),
        age_seconds: ageSeconds,
        stale,
      },
    };
  }

  function requireSession(): Session {
    if (session === null) {
      throw new ApiError('No active rounding session', 404);
    }
    return session;
  }

  function view(s: Session): RoundView {
    return { current: buildCard(s.current), order: [...s.order] };
  }

  function findFact(patient: CohortPatient, message: string): ChatFact | null {
    const q = message.toLowerCase();
    for (const fact of patient.facts) {
      if (fact.topics.some((topic) => q.includes(topic))) {
        return fact;
      }
    }
    return null;
  }

  function answerFor(patient: CohortPatient, message: string): {
    answer: string;
    claims: Claim[];
    action: 'served' | 'withheld' | 'degraded';
    passed: boolean;
  } {
    const fact = findFact(patient, message);
    if (fact && fact.requiresDeterioration && !deteriorationFired) {
      if (fact.answerBefore !== undefined) {
        return {
          answer: fact.answerBefore,
          claims: fact.claimsBefore ?? [],
          action: 'served',
          passed: true,
        };
      }
      // Nothing on file yet for this topic — fail closed.
      return withheld(patient);
    }
    if (fact) {
      return {
        answer: fact.answer,
        claims: fact.claims,
        action: fact.action,
        passed: fact.action === 'served',
      };
    }
    return withheld(patient);
  }

  function withheld(patient: CohortPatient): {
    answer: string;
    claims: Claim[];
    action: 'withheld';
    passed: false;
  } {
    return {
      answer:
        `I can't confirm that from ${patientName(patient.id)}'s record. ` +
        `I checked labs, medication orders, allergies, and conditions as of ${clock(new Date())} ` +
        `and found no source for it. Rather than guess, I'm withholding an answer.`,
      claims: [],
      action: 'withheld',
      passed: false,
    };
  }

  return {
    mode: 'mock',

    async startRound(_clinicianId, patientIds) {
      await delay(jitter(260, 240));
      const known = patientIds.filter((id) => cohortById.has(id));
      if (known.length === 0) {
        throw new ApiError('No patients on the rounding list', 422);
      }
      const order = [...known].sort((a, b) => {
        const pa = phaseFor(requirePatient(a)).acuity;
        const pb = phaseFor(requirePatient(b)).acuity;
        return pb - pa;
      });
      const current = order[0];
      if (current === undefined) {
        throw new ApiError('No patients on the rounding list', 422);
      }
      // Note: deterioration state persists across restarts — records do not
      // un-happen. A second pass ranks (and shows) 1005 at her raised acuity.
      session = { order, seen: new Set(), current, startedAt: Date.now() };
      recheckedAt.clear();
      return view(session);
    },

    async currentCard(_clinicianId) {
      await delay(jitter(160, 160));
      maybeFireDeterioration();
      return view(requireSession());
    },

    async advance(_clinicianId, completedPatientId) {
      await delay(jitter(260, 240));
      maybeFireDeterioration();
      const s = requireSession();
      if (completedPatientId !== s.current) {
        throw new ApiError('completed_patient_id is not the current patient', 409);
      }
      s.seen.add(s.current);
      const next = s.order.find((id) => !s.seen.has(id));
      if (next === undefined) {
        return { done: true };
      }
      s.current = next;
      return view(s);
    },

    async refresh(_clinicianId) {
      await delay(jitter(700, 500));
      const s = requireSession();
      // A manual re-check pulls whatever is new — including the lactate.
      const firedNow = !deteriorationFired && cohortById.has(DETERIORATING_PATIENT);
      if (firedNow) {
        deteriorationFired = true;
      }
      const now = Date.now();
      const outcomes: RefreshOutcome[] = s.order.map((id) => {
        recheckedAt.set(id, now);
        const changed = id === DETERIORATING_PATIENT && firedNow;
        return { patient_id: id, outcome: changed ? 'updated' : 'unchanged' };
      });
      return outcomes;
    },

    async alerts(_clinicianId) {
      await delay(jitter(120, 120));
      maybeFireDeterioration();
      const s = session;
      if (s === null || !deteriorationFired) {
        return [];
      }
      if (s.seen.has(DETERIORATING_PATIENT) || !s.order.includes(DETERIORATING_PATIENT)) {
        return [];
      }
      const alert: DeteriorationAlert = {
        patient_id: DETERIORATING_PATIENT,
        reason: ALERT_REASON_1005,
      };
      return [alert];
    },

    async jumpTo(_clinicianId, patientId, _unseenIds) {
      await delay(jitter(260, 200));
      const s = requireSession();
      requirePatient(patientId);
      if (s.seen.has(patientId)) {
        throw new ApiError('Patient already seen this round', 409);
      }
      if (patientId !== s.current) {
        const seenPart = s.order.filter((id) => s.seen.has(id));
        const rest = s.order.filter(
          (id) => !s.seen.has(id) && id !== patientId && id !== s.current,
        );
        s.order = [...seenPart, patientId, s.current, ...rest];
        s.current = patientId;
      }
      return view(s);
    },

    async chat(req: ChatRequest): Promise<ChatResponse> {
      await delay(jitter(650, 550));
      maybeFireDeterioration();
      const patient = requirePatient(req.patient_id);
      const result = answerFor(patient, req.message);

      const conversationId = req.conversation_id ?? nextConversationId++;
      const log = conversations.get(conversationId) ?? [];
      log.push({ role: 'clinician', content: req.message });
      log.push({ role: 'copilot', content: result.answer });
      conversations.set(conversationId, log);

      return {
        answer: result.answer,
        claims: result.claims,
        verification: { action: result.action, passed: result.passed },
        conversation_id: conversationId,
        correlation_id: req.correlation_id ?? `mock-${newCorrelationId()}`,
      };
    },

    async getConversation(conversationId) {
      await delay(jitter(120, 100));
      const log = conversations.get(conversationId);
      if (!log) {
        throw new ApiError('Conversation not found', 404);
      }
      return [...log];
    },

    async observations(_clinicianId, patientId, metric) {
      await delay(jitter(240, 220));
      const patient = requirePatient(patientId);
      const empty = { patient_id: patientId, metric, unit: '', reference_range: null, points: [] };
      const catalogue = patient.series;
      if (catalogue === undefined) {
        return empty;
      }
      const key = metric.trim().toLowerCase();
      const match = Object.entries(catalogue).find(([name]) => {
        const n = name.toLowerCase();
        // Tolerant match: the drill-down may hand us a fuller phrase than the
        // canonical label (e.g. "Heart rate trending up" vs "Heart rate").
        return n === key || key.startsWith(n) || n.startsWith(key);
      });
      if (match === undefined) {
        // Unknown metric — an honest empty series, never fabricated.
        return empty;
      }
      return { ...match[1], patient_id: patientId };
    },

    async proposeWrite(clinicianId, patientId, kind, metric, rawValue, unit) {
      await delay(jitter(420, 320));
      requirePatient(patientId);

      const spec = WRITABLE_METRICS[metric as WritableMetric] as
        | (typeof WRITABLE_METRICS)[WritableMetric]
        | undefined;
      if (spec === undefined) {
        // Unknown metric — hard block, exactly like the server-side gate.
        throw new WriteRejectedError([`"${metric}" is not an editable vital.`]);
      }
      if (unit !== spec.unit) {
        throw new WriteRejectedError([
          `Unit for ${spec.label.toLowerCase()} must be ${spec.unit}, not "${unit}".`,
        ]);
      }
      const value = Number(rawValue.trim());
      if (rawValue.trim() === '' || !Number.isFinite(value)) {
        throw new WriteRejectedError([`"${rawValue}" is not a numeric ${spec.label.toLowerCase()}.`]);
      }

      // Out-of-physiologic-range is a SOFT warning for a human direct-edit —
      // surfaced, still confirmable — never a hard block.
      const warnings =
        value < spec.min || value > spec.max
          ? [
              `${value} ${spec.unit} is outside the usual range ` +
                `(${spec.min}–${spec.max} ${spec.unit}). Confirm this is correct.`,
            ]
          : [];

      const candidate: WriteCandidate = {
        kind,
        patient_id: { value: patientId },
        clinician_id: { value: clinicianId },
        idempotency_key: `mock-${newCorrelationId()}`,
        entry_mode: 'human_direct',
        vital: { metric: spec.metric, value, unit: spec.unit },
        medication: null,
      };

      const proposed: ProposedWrite = {
        candidate,
        verdict: {
          kind,
          metric: spec.metric,
          blocked: false,
          warnings,
          errors: [],
        },
        effective_time: new Date().toISOString(),
        notice:
          'This creates a NEW record dated now; it does not overwrite prior values.',
      };
      return proposed;
    },

    async confirmWrite(_clinicianId, _patientId, candidate, idempotencyKey) {
      await delay(jitter(520, 360));
      // Idempotent: a retried/double-clicked confirm returns the same record.
      const existing = committedWrites.get(idempotencyKey);
      if (existing !== undefined) {
        return existing;
      }
      const suffix = Math.floor(Math.random() * 9000 + 1000);
      const committed: CommittedWrite = {
        resource_kind: candidate.kind === 'medication' ? 'medication' : 'vital',
        new_id: `${candidate.kind}-${suffix}`,
        encounter_id: candidate.kind === 'medication' ? null : `enc-${suffix}`,
        committed_at: new Date().toISOString(),
      };
      committedWrites.set(idempotencyKey, committed);
      return committed;
    },

    async uploadDocument(patientId, _file, _docType) {
      // Simulated ingestion: the 202 acknowledgement, offline. No extraction
      // ever materializes in mock mode — status stays honest ("processing").
      await delay(jitter(600, 500));
      requirePatient(patientId);
      const accepted: DocumentAccepted = {
        document_id: `mock-doc-${newCorrelationId()}`,
        status: 'processing',
        correlation_id: `mock-${newCorrelationId()}`,
      };
      return accepted;
    },
  };
}
