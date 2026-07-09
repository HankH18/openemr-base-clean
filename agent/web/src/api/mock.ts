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
import {
  ApiError,
  type ChatRequest,
  type ChatResponse,
  type Claim,
  type ConversationMessage,
  type DeteriorationAlert,
  type PatientCard,
  type RefreshOutcome,
  type RoundView,
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
        correlation_id: req.correlation_id ?? `mock-${crypto.randomUUID()}`,
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
  };
}
