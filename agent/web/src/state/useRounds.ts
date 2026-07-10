import { useCallback, useEffect, useRef, useState } from 'react';
import type { CopilotApi } from '../api/client';
import type { PatientCard, RefreshOutcome, RoundView } from '../api/types';

export type RoundsPhase = 'loading' | 'active' | 'complete' | 'error';

export interface RoundsController {
  phase: RoundsPhase;
  card: PatientCard | null;
  /** Backend-ranked patient order (risk/acuity descending, sickest first). */
  order: number[];
  seen: number[];
  /** Non-fatal operation failure, surfaced inline and cleared on next success. */
  notice: string | null;
  busy: boolean;
  advance: () => Promise<void>;
  jumpTo: (patientId: number) => Promise<void>;
  recheck: () => Promise<RefreshOutcome[] | null>;
  restart: () => Promise<void>;
}

function opFailed(action: string): string {
  return `${action} failed — the record service did not respond. Nothing was changed.`;
}

export function useRounds(api: CopilotApi, clinicianId: number, patientIds: number[]): RoundsController {
  const [phase, setPhase] = useState<RoundsPhase>('loading');
  const [card, setCard] = useState<PatientCard | null>(null);
  const [order, setOrder] = useState<number[]>([]);
  const [seen, setSeen] = useState<number[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);
  const seenRef = useRef<number[]>([]);
  const cardRef = useRef<PatientCard | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const commitSeen = useCallback((ids: number[]) => {
    seenRef.current = ids;
    setSeen(ids);
  }, []);

  const commitView = useCallback((view: RoundView) => {
    cardRef.current = view.current;
    setCard(view.current);
    setOrder(view.order); // backend order is already risk-sorted; show as-is
    setNotice(null);
  }, []);

  const start = useCallback(async () => {
    setPhase('loading');
    setNotice(null);
    commitSeen([]);
    try {
      const view = await api.startRound(clinicianId, patientIds);
      if (!mounted.current) {
        return;
      }
      commitView(view);
      setPhase('active');
    } catch {
      if (mounted.current) {
        setPhase('error');
      }
    }
  }, [api, clinicianId, patientIds, commitSeen, commitView]);

  useEffect(() => {
    void start();
  }, [start]);

  const advance = useCallback(async () => {
    const current = cardRef.current;
    if (current === null || busy) {
      return;
    }
    setBusy(true);
    try {
      const result = await api.advance(clinicianId, current.patient_id);
      if (!mounted.current) {
        return;
      }
      commitSeen([...seenRef.current, current.patient_id]);
      if ('done' in result) {
        cardRef.current = null;
        setCard(null);
        setPhase('complete');
        setNotice(null);
      } else {
        commitView(result);
      }
    } catch {
      if (mounted.current) {
        setNotice(opFailed('Advancing'));
      }
    } finally {
      if (mounted.current) {
        setBusy(false);
      }
    }
  }, [api, clinicianId, busy, commitSeen, commitView]);

  const jumpTo = useCallback(
    async (patientId: number) => {
      const current = cardRef.current;
      if (busy || current === null || current.patient_id === patientId) {
        return;
      }
      setBusy(true);
      try {
        const seenIds = seenRef.current;
        const unseen = [current.patient_id, ...order.filter(
          (id) => !seenIds.includes(id) && id !== current.patient_id && id !== patientId,
        )];
        const view = await api.jumpTo(clinicianId, patientId, unseen);
        if (mounted.current) {
          commitView(view);
        }
      } catch {
        if (mounted.current) {
          setNotice(opFailed('Jumping to the patient'));
        }
      } finally {
        if (mounted.current) {
          setBusy(false);
        }
      }
    },
    [api, clinicianId, busy, order, commitView],
  );

  const recheck = useCallback(async (): Promise<RefreshOutcome[] | null> => {
    try {
      const outcomes = await api.refresh(clinicianId);
      if (!mounted.current) {
        return outcomes;
      }
      // Freshness (and possibly acuity) moved — re-read the current card.
      if (cardRef.current !== null) {
        const view = await api.currentCard(clinicianId);
        if (mounted.current) {
          commitView(view);
        }
      }
      return outcomes;
    } catch {
      if (mounted.current) {
        setNotice(opFailed('Re-checking the charts'));
      }
      return null;
    }
  }, [api, clinicianId, commitView]);

  const restart = useCallback(async () => {
    await start();
  }, [start]);

  return { phase, card, order, seen, notice, busy, advance, jumpTo, recheck, restart };
}
