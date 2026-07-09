import { acuitySeverity, fmtAcuity } from '../fmt';

const TICKS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];

/**
 * Acuity 0–10 as a numeral plus a ten-segment calibration bar.
 * Low acuity renders in neutral ink — green is reserved for verification.
 */
export function AcuityMeter({ score }: { score: number }): JSX.Element {
  const severity = acuitySeverity(score);
  const filled = Math.max(0, Math.min(10, Math.round(score)));
  return (
    <div className={`acuity acuity--${severity}`}>
      <div className="acuity-num" aria-hidden="true">
        <span className="acuity-score">{fmtAcuity(score)}</span>
        <span className="acuity-max">/10</span>
      </div>
      <div
        className="acuity-ticks"
        role="img"
        aria-label={`Acuity ${fmtAcuity(score)} out of 10 (${severity})`}
      >
        {TICKS.map((i) => (
          <span key={i} className={i < filled ? 'tick tick--on' : 'tick'} />
        ))}
      </div>
    </div>
  );
}
