import { Button } from 'react-aria-components';
import type { DeteriorationAlert } from '../api/types';
import { censusEntry } from '../census';

/**
 * Non-modal deterioration offer. The physician decides: jump, or stay.
 * It never steals focus and never blocks the page.
 */
export function AlertBanner({
  alert,
  busy,
  onAccept,
  onDismiss,
}: {
  alert: DeteriorationAlert;
  busy: boolean;
  onAccept: () => void;
  onDismiss: () => void;
}): JSX.Element {
  const entry = censusEntry(alert.patient_id);
  const name = entry?.name ?? `Patient ${alert.patient_id}`;
  return (
    <div className="alert-banner" role="status" aria-live="polite">
      <span className="alert-kicker">Deterioration</span>
      <p className="alert-body">
        <strong>{name}</strong>
        {entry ? ` — Bed ${entry.bed}` : ''} · {alert.reason}
      </p>
      <div className="alert-actions">
        <Button className="btn btn--critical" onPress={onAccept} isDisabled={busy}>
          Jump to {entry?.given ?? name}
        </Button>
        <Button className="btn btn--quiet" onPress={onDismiss}>
          Stay with current patient
        </Button>
      </div>
    </div>
  );
}
