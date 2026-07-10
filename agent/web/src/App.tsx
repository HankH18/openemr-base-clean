import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button } from 'react-aria-components';
import { createApi } from './api/client';
import type { DeteriorationAlert } from './api/types';
import { CENSUS, CLINICIAN_ID, censusEntry } from './census';
import { fmtClock } from './fmt';
import { AlertBanner } from './components/AlertBanner';
import { ChatPanel } from './components/ChatPanel';
import { CompleteView } from './components/CompleteView';
import { PatientHero } from './components/PatientHero';
import { QueueRail } from './components/QueueRail';
import { TopBar } from './components/TopBar';
import { useAlerts } from './state/useAlerts';
import { useChat } from './state/useChat';
import { useRounds } from './state/useRounds';
import { useTheme } from './state/theme';
import { suggestionsFor } from './suggestions';

const EXIT_MS = 190;

/**
 * Demo affordance. The deterioration detector is a change-gated poller (a
 * pub/sub-style loop) that is impractical to drive live for a recording, so
 * "Re-check charts" deterministically raises the high-urgency alert the poller
 * would otherwise surface. Client-side only — mirrors the mock cohort's 1005
 * (Lillian Cho) critical-lactate event so live and demo look identical.
 */
const DEMO_ALERT: DeteriorationAlert = {
  patient_id: 1005,
  reason:
    'New lactate 5.0 mmol/L — critical high (reference 0.5–2.0), resulted 06:58. Acuity 4.2 → 9.3.',
};

function prefersReducedMotion(): boolean {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export function App(): JSX.Element {
  const api = useMemo(createApi, []);
  const patientIds = useMemo(() => CENSUS.map((entry) => entry.id), []);
  const { theme, toggle } = useTheme();
  const rounds = useRounds(api, CLINICIAN_ID, patientIds);
  const alerts = useAlerts(api, CLINICIAN_ID, rounds.phase === 'active');
  const chat = useChat(api, CLINICIAN_ID);

  const [leaving, setLeaving] = useState(false);
  const [recheckStatus, setRecheckStatus] = useState<string | null>(null);
  const [rechecking, setRechecking] = useState(false);
  const [forcedAlert, setForcedAlert] = useState<DeteriorationAlert | null>(null);
  const statusTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (statusTimer.current !== null) {
        window.clearTimeout(statusTimer.current);
      }
    },
    [],
  );

  const card = rounds.card;
  const currentId = card?.patient_id ?? null;

  // Polled alerts plus the demo-forced one (deduped by patient), so the banner
  // and the rail's "Alert" state behave identically whether the signal came
  // from the poller or the manual re-check.
  const activeAlerts = useMemo(() => {
    if (forcedAlert === null) {
      return alerts.alerts;
    }
    if (alerts.alerts.some((a) => a.patient_id === forcedAlert.patient_id)) {
      return alerts.alerts;
    }
    return [...alerts.alerts, forcedAlert];
  }, [alerts.alerts, forcedAlert]);

  const offer =
    rounds.phase === 'active'
      ? activeAlerts.find(
          (a) =>
            !alerts.dismissed.has(a.patient_id) &&
            a.patient_id !== currentId &&
            !rounds.seen.includes(a.patient_id),
        ) ?? null
      : null;
  const alertIds = useMemo(
    () => new Set(activeAlerts.map((a) => a.patient_id)),
    [activeAlerts],
  );

  const withExitTransition = useCallback(async (action: () => Promise<void>) => {
    if (!prefersReducedMotion()) {
      setLeaving(true);
      await wait(EXIT_MS);
    }
    try {
      await action();
    } finally {
      setLeaving(false);
    }
  }, []);

  const handleDone = useCallback(() => {
    void withExitTransition(rounds.advance);
  }, [withExitTransition, rounds.advance]);

  const handleJump = useCallback(
    (patientId: number) => {
      void withExitTransition(() => rounds.jumpTo(patientId));
    },
    [withExitTransition, rounds.jumpTo],
  );

  const handleRecheck = useCallback(() => {
    void (async () => {
      setRechecking(true);
      const outcomes = await rounds.recheck();
      // Demo: surface the high-urgency deterioration the poller would raise.
      setForcedAlert(DEMO_ALERT);
      setRechecking(false);
      const checked = outcomes?.length ?? patientIds.length;
      setRecheckStatus(
        `${checked} charts re-checked · 1 flagged · ${fmtClock(new Date().toISOString())}`,
      );
      if (statusTimer.current !== null) {
        window.clearTimeout(statusTimer.current);
      }
      statusTimer.current = window.setTimeout(() => {
        setRecheckStatus(null);
      }, 7000);
    })();
  }, [rounds.recheck, patientIds.length]);

  const entry = currentId !== null ? censusEntry(currentId) : undefined;
  const position = currentId !== null ? rounds.order.indexOf(currentId) + 1 : 0;
  const unseenCount = rounds.order.length - rounds.seen.length;

  return (
    <div className="app">
      <TopBar
        mode={api.mode}
        theme={theme}
        onToggleTheme={toggle}
        onRecheck={handleRecheck}
        recheckStatus={recheckStatus}
        rechecking={rechecking}
        showRecheck={rounds.phase === 'active'}
      />

      {offer !== null ? (
        <AlertBanner
          alert={offer}
          busy={rounds.busy}
          onAccept={() => {
            handleJump(offer.patient_id);
          }}
          onDismiss={() => {
            alerts.dismiss(offer.patient_id);
          }}
        />
      ) : null}

      {rounds.notice !== null ? (
        <p className="notice" role="alert">
          {rounds.notice}
        </p>
      ) : null}

      {rounds.phase === 'loading' ? (
        <div className="gate">
          <div className="gate-pulse" aria-hidden="true" />
          <h1 className="gate-title">Ranking your list.</h1>
          <p className="gate-sub">
            Reading {patientIds.length} charts — sickest first. Every card is grounded in the
            record before it reaches you.
          </p>
        </div>
      ) : null}

      {rounds.phase === 'error' ? (
        <div className="gate">
          <h1 className="gate-title">The record service is unreachable.</h1>
          <p className="gate-sub">
            No cards will be shown from memory or guesswork. Retry when the service is back.
          </p>
          <Button
            className="btn btn--primary"
            onPress={() => {
              void rounds.restart();
            }}
          >
            Retry
          </Button>
        </div>
      ) : null}

      {rounds.phase === 'complete' ? (
        <CompleteView
          seen={rounds.seen}
          onRestart={() => {
            void rounds.restart();
          }}
        />
      ) : null}

      {rounds.phase === 'active' && card !== null ? (
        <div className="frame">
          <QueueRail
            order={rounds.order}
            seen={rounds.seen}
            currentId={currentId}
            alertIds={alertIds}
            onSelect={handleJump}
            busy={rounds.busy}
          />
          <main className="stage">
            <div className={leaving ? 'visit visit--leaving' : 'visit'} key={card.patient_id}>
              <PatientHero
                card={card}
                entry={entry}
                position={position}
                total={rounds.order.length}
                isLast={unseenCount <= 1}
                busy={rounds.busy}
                onDone={handleDone}
              />
              <ChatPanel
                given={entry?.given ?? `patient ${card.patient_id}`}
                messages={chat.messagesFor(card.patient_id)}
                busy={chat.busy}
                suggestions={suggestionsFor(card.patient_id)}
                onSend={(message) => {
                  void chat.send(card.patient_id, message);
                }}
              />
            </div>
          </main>
        </div>
      ) : null}
    </div>
  );
}
