import { useCallback, useEffect, useState } from 'react';
import type { CopilotApi } from '../api/client';
import type { DeteriorationAlert } from '../api/types';

const POLL_MS = 5000;

export interface AlertsController {
  alerts: DeteriorationAlert[];
  dismissed: ReadonlySet<number>;
  dismiss: (patientId: number) => void;
}

/**
 * Polls /v1/rounds/alerts while a round is active. Raw alerts are returned;
 * the caller filters against current/seen state at render time (so the
 * filter never goes stale inside the polling closure).
 */
export function useAlerts(api: CopilotApi, clinicianId: number, enabled: boolean): AlertsController {
  const [alerts, setAlerts] = useState<DeteriorationAlert[]>([]);
  const [dismissed, setDismissed] = useState<ReadonlySet<number>>(new Set());

  useEffect(() => {
    if (!enabled) {
      setAlerts([]);
      return;
    }
    let cancelled = false;

    const poll = async (): Promise<void> => {
      try {
        const found = await api.alerts(clinicianId);
        if (!cancelled) {
          setAlerts(found);
        }
      } catch {
        /* polling is best-effort; the next tick retries */
      }
    };

    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, POLL_MS);

    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [api, clinicianId, enabled]);

  const dismiss = useCallback((patientId: number) => {
    setDismissed((prev) => {
      const next = new Set(prev);
      next.add(patientId);
      return next;
    });
  }, []);

  return { alerts, dismissed, dismiss };
}
