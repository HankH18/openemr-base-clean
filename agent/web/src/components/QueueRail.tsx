import { Button } from 'react-aria-components';
import { censusEntry } from '../census';

type RowState = 'seen' | 'now' | 'alert' | 'next' | 'upcoming';

function rowState(
  id: number,
  currentId: number | null,
  seen: number[],
  alertIds: ReadonlySet<number>,
  nextId: number | null,
): RowState {
  if (seen.includes(id)) {
    return 'seen';
  }
  if (id === currentId) {
    return 'now';
  }
  if (alertIds.has(id)) {
    return 'alert';
  }
  // Only the single patient the physician visits next is "Up next"; the rest
  // of the unseen list is "Upcoming". (Previously every unseen row read
  // "Up next", which said nothing about order.)
  if (id === nextId) {
    return 'next';
  }
  return 'upcoming';
}

const STATUS_LABEL: Record<RowState, string> = {
  seen: 'Seen',
  now: 'Now',
  alert: 'Alert',
  next: 'Up next',
  upcoming: 'Upcoming',
};

/**
 * The census strip — the round's visiting order, sickest first. Rank
 * numerals are shown because the queue genuinely is a sequence; per-patient
 * acuity numbers are not invented here — they come only from cards the
 * service has issued.
 *
 * Each row is a button: selecting a patient jumps the round to them. The
 * current patient's row is inert (you are already there).
 */
export function QueueRail({
  order,
  seen,
  currentId,
  alertIds,
  onSelect,
  busy,
}: {
  order: number[];
  seen: number[];
  currentId: number | null;
  alertIds: ReadonlySet<number>;
  onSelect: (patientId: number) => void;
  busy?: boolean;
}): JSX.Element {
  // The next patient to visit: the first unseen patient after the current one
  // in the visiting order. Exactly one row is labelled "Up next".
  const currentIdx = currentId === null ? -1 : order.indexOf(currentId);
  const nextId =
    currentIdx >= 0
      ? order.slice(currentIdx + 1).find((id) => !seen.includes(id)) ?? null
      : order.find((id) => !seen.includes(id)) ?? null;
  return (
    <aside className="rail" aria-label="Rounding order">
      <div className="rail-head">
        <h2 className="rail-title">Rounding order</h2>
        <span className="rail-progress">
          {seen.length} of {order.length} seen
        </span>
      </div>
      <ol className="queue">
        {order.map((id, index) => {
          const entry = censusEntry(id);
          const state = rowState(id, currentId, seen, alertIds, nextId);
          const name = entry?.name ?? `Patient ${id}`;
          return (
            <li
              key={id}
              className={`q-row q-row--${state}`}
              aria-current={state === 'now' ? 'true' : undefined}
            >
              <Button
                className="q-row-btn"
                isDisabled={Boolean(busy) || state === 'now'}
                onPress={() => onSelect(id)}
                aria-label={state === 'now' ? `${name} — current patient` : `Go to ${name}`}
              >
                <span className="q-rank">{String(index + 1).padStart(2, '0')}</span>
                <span className="q-body">
                  <span className="q-name">{name}</span>
                  <span className="q-meta">
                    {entry ? `Bed ${entry.bed} · ${entry.service}` : `MRN ${id}`}
                  </span>
                </span>
                <span className={`q-status q-status--${state}`}>{STATUS_LABEL[state]}</span>
              </Button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
