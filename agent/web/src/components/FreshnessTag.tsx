import type { Freshness } from '../api/types';
import { fmtAge, fmtClock } from '../fmt';

/** Synthesis watermark: when the card's facts were last confirmed against the record. */
export function FreshnessTag({ freshness }: { freshness: Freshness }): JSX.Element {
  return (
    <div className={freshness.stale ? 'freshness freshness--stale' : 'freshness'}>
      <span>
        as of {fmtClock(freshness.as_of)} · {fmtAge(freshness.age_seconds)}
      </span>
      {freshness.stale ? <span className="stale-tag">Stale — re-check advised</span> : null}
    </div>
  );
}
