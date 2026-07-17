import { useCallback, useRef, useState } from 'react';
import type { CopilotApi } from '../api/client';
import type { Claim, GuidelineEvidenceItem, Verification } from '../api/types';
import { newCorrelationId } from '../ids';

export interface ChatMessage {
  id: string;
  kind: 'question' | 'answer' | 'error';
  text: string;
  claims: Claim[];
  /** Separate guideline backing — literature, never patient-fact claims. */
  guidelineEvidence: GuidelineEvidenceItem[];
  verification: Verification | null;
  correlationId: string | null;
  pending: boolean;
}

export interface ChatController {
  messagesFor: (patientId: number) => ChatMessage[];
  send: (patientId: number, message: string) => Promise<void>;
  busy: boolean;
}

let messageSeq = 0;

function nextId(): string {
  messageSeq += 1;
  return `msg-${messageSeq}`;
}

export function useChat(api: CopilotApi, clinicianId: number): ChatController {
  const [threads, setThreads] = useState<Record<number, ChatMessage[]>>({});
  const [busy, setBusy] = useState(false);
  const conversationIds = useRef(new Map<number, number>());

  const append = useCallback((patientId: number, message: ChatMessage) => {
    setThreads((prev) => ({
      ...prev,
      [patientId]: [...(prev[patientId] ?? []), message],
    }));
  }, []);

  const replace = useCallback((patientId: number, messageId: string, message: ChatMessage) => {
    setThreads((prev) => ({
      ...prev,
      [patientId]: (prev[patientId] ?? []).map((m) => (m.id === messageId ? message : m)),
    }));
  }, []);

  const messagesFor = useCallback(
    (patientId: number): ChatMessage[] => threads[patientId] ?? [],
    [threads],
  );

  const send = useCallback(
    async (patientId: number, raw: string) => {
      const message = raw.trim();
      if (message === '' || busy) {
        return;
      }
      setBusy(true);
      append(patientId, {
        id: nextId(),
        kind: 'question',
        text: message,
        claims: [],
        guidelineEvidence: [],
        verification: null,
        correlationId: null,
        pending: false,
      });
      const placeholderId = nextId();
      append(patientId, {
        id: placeholderId,
        kind: 'answer',
        text: 'Checking the record…',
        claims: [],
        guidelineEvidence: [],
        verification: null,
        correlationId: null,
        pending: true,
      });
      try {
        const response = await api.chat({
          clinician_id: clinicianId,
          patient_id: patientId,
          message,
          ...(conversationIds.current.has(patientId)
            ? { conversation_id: conversationIds.current.get(patientId) as number }
            : {}),
          correlation_id: newCorrelationId(),
        });
        conversationIds.current.set(patientId, response.conversation_id);
        replace(patientId, placeholderId, {
          id: placeholderId,
          kind: 'answer',
          text: response.answer,
          claims: response.claims,
          guidelineEvidence: response.guideline_evidence ?? [],
          verification: response.verification,
          correlationId: response.correlation_id,
          pending: false,
        });
      } catch {
        replace(patientId, placeholderId, {
          id: placeholderId,
          kind: 'error',
          text: 'The record service did not respond — this question was not answered. Nothing is inferred without a source.',
          claims: [],
          guidelineEvidence: [],
          verification: null,
          correlationId: null,
          pending: false,
        });
      } finally {
        setBusy(false);
      }
    },
    [api, clinicianId, busy, append, replace],
  );

  return { messagesFor, send, busy };
}
