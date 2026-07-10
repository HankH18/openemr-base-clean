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
 * would otherwise surface. Client-side only. Targets June Okafor (1004), whose
 * LIVE card actually carries the cited critical lactate, so jumping to the
 * alert lands on a card that corroborates it — the round opens on Marcus (1003,
 * DKA), so she is always unseen and not current when the alert fires.
 */
const DEMO_ALERT: DeteriorationAlert = {
  patient_id: 1004,
  reason:
    'New lactate 4.2 mmol/L — critical high (reference 0.5–2.0). Concern for septic shock — acuity now 9.3.',
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
  // Polling is disabled: the backend /alerts feed flags every not-yet-seen
  // patient at/above the acuity threshold, which fires on first paint for
  // statically-critical patients — but criticality is the queue RANKING's job,
  // not a deterioration banner. A deterioration is a *change* during the round,
  // surfaced deterministically by "Re-check charts" (see handleRecheck). We keep
  // the hook only for its dismiss/dismissed state.
  const alerts = useAlerts(api, CLINICIAN_ID, false);
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

  // The only deterioration surfaced is the one raised by "Re-check charts" — a
  // genuine mid-round change, not a standing "this patient is critical" (that is
  // what the ranking conveys). Empty until the physician re-checks.
  const activeAlerts = useMemo(
    () => (forcedAlert === null ? [] : [forcedAlert]),
    [forcedAlert],
  );

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

  // Rail/hero display order. The patient the doctor is currently seeing always
  // leads the queue; everyone else reshuffles beneath them. Among those others,
  // a patient whose risk just spiked (active alert) rises to the top; the rest
  // keep the backend's risk ranking, and already-seen patients sink to the
  // bottom. Kept out of useRounds so round state stays the pure backend ranking.
  const displayOrder = useMemo(() => {
    const seen = rounds.seen;
    const unseen = rounds.order.filter((id) => !seen.includes(id));
    const head = currentId !== null && unseen.includes(currentId) ? [currentId] : [];
    const others = unseen.filter((id) => id !== currentId);
    const alerted = others.filter((id) => alertIds.has(id)); // spiked risk -> just below current
    const rest = others.filter((id) => !alertIds.has(id)); // backend acuity order preserved
    const seenList = rounds.order.filter((id) => seen.includes(id)); // done patients sink to bottom
    return [...head, ...alerted, ...rest, ...seenList];
  }, [rounds.order, rounds.seen, alertIds, currentId]);

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
  const position = currentId !== null ? displayOrder.indexOf(currentId) + 1 : 0;
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
            order={displayOrder}
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
                total={displayOrder.length}
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
