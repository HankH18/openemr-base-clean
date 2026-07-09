import { censusEntry } from '../census';

type RowState = 'seen' | 'now' | 'up' | 'alert';

function rowState(
  id: number,
  currentId: number | null,
  seen: number[],
  alertIds: ReadonlySet<number>,
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
  return 'up';
}

const STATUS_LABEL: Record<RowState, string> = {
  seen: 'Seen',
  now: 'Now',
  up: 'Up next',
  alert: 'Alert',
};

/**
 * The census strip — the round's visiting order, sickest first. Rank
 * numerals are shown because the queue genuinely is a sequence; per-patient
 * acuity numbers are not invented here — they come only from cards the
 * service has issued.
 */
export function QueueRail({
  order,
  seen,
  currentId,
  alertIds,
}: {
  order: number[];
  seen: number[];
  currentId: number | null;
  alertIds: ReadonlySet<number>;
}): JSX.Element {
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
          const state = rowState(id, currentId, seen, alertIds);
          return (
            <li
              key={id}
              className={`q-row q-row--${state}`}
              aria-current={state === 'now' ? 'true' : undefined}
            >
              <span className="q-rank">{String(index + 1).padStart(2, '0')}</span>
              <span className="q-body">
                <span className="q-name">{entry?.name ?? `Patient ${id}`}</span>
                <span className="q-meta">
                  {entry ? `Bed ${entry.bed} · ${entry.service}` : `MRN ${id}`}
                </span>
              </span>
              <span className={`q-status q-status--${state}`}>{STATUS_LABEL[state]}</span>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
