import type { ObservationSeries, ObservationSeriesPoint, ReferenceRange } from '../api/types';
import { fmtStamp } from '../fmt';

/**
 * A hand-rolled inline SVG line chart for a single metric's trend — no chart
 * library. One series (change-over-time), so no legend box: the header names
 * it. Consumes the design tokens directly, so it is theme-aware in light and
 * dark, and the line-draw animation is gated on `prefers-reduced-motion`.
 *
 * Geometry: a fixed 360×190 viewBox scaled to 100% width (never overflows its
 * popover). Time on x (falls back to even index spacing when timestamps are
 * missing/degenerate), value on y with the reference range folded into the
 * domain so the band is always visible. A shaded reference band, a 2px accent
 * polyline, ≥8px point markers (out-of-range points wear the reserved
 * critical/warning status tokens), the endpoint value direct-labeled, and
 * sparse first/last time ticks.
 *
 * Accessibility: role="img" + an aria-label summarizing the trend, a <title>,
 * and a visually-hidden data table for screen readers.
 */

const W = 360;
const H = 190;
const M = { top: 16, right: 48, bottom: 26, left: 42 };
const PX0 = M.left;
const PX1 = W - M.right;
const PY0 = M.top;
const PY1 = H - M.bottom;

type Severity = 'normal' | 'warning' | 'critical';

function severityOf(point: ObservationSeriesPoint, range: ReferenceRange | null): Severity {
  const flag = point.abnormal.trim().toLowerCase();
  if (flag !== '') {
    if (
      flag.includes('crit') ||
      flag.includes('panic') ||
      flag.startsWith('vh') ||
      flag.startsWith('vl') ||
      flag === 'hh' ||
      flag === 'll' ||
      flag === '<<' ||
      flag === '>>'
    ) {
      return 'critical';
    }
    return 'warning';
  }
  if (range !== null) {
    const v = Number(point.value);
    if (Number.isFinite(v) && (v < range.low || v > range.high)) {
      return 'warning';
    }
  }
  return 'normal';
}

function pointClass(sev: Severity): string {
  if (sev === 'critical') {
    return 'metric-pt metric-pt--critical';
  }
  if (sev === 'warning') {
    return 'metric-pt metric-pt--warning';
  }
  return 'metric-pt';
}

/** Compact numeric label for the axis (drops noise, keeps the recorded form). */
function numLabel(n: number): string {
  return String(n);
}

export function MetricChart({ series }: { series: ObservationSeries }): JSX.Element {
  const { metric, unit, reference_range: range } = series;

  const parsed = series.points
    .map((point) => ({ point, v: Number(point.value), t: Date.parse(point.timestamp) }))
    .filter((d) => Number.isFinite(d.v));

  const first = parsed[0];
  const last = parsed[parsed.length - 1];
  if (parsed.length === 0 || first === undefined || last === undefined) {
    return <p className="trend-empty">No plottable readings for {metric}.</p>;
  }

  const n = parsed.length;
  const times = parsed.map((d) => d.t);
  const tMin = Math.min(...times);
  const tMax = Math.max(...times);
  const useTime = times.every((t) => Number.isFinite(t)) && tMax > tMin;

  // y-domain: data extent unioned with the reference range, then padded.
  const domainNums = parsed.map((d) => d.v);
  if (range !== null) {
    domainNums.push(range.low, range.high);
  }
  let vMin = Math.min(...domainNums);
  let vMax = Math.max(...domainNums);
  if (vMin === vMax) {
    vMin -= 1;
    vMax += 1;
  }
  const padY = (vMax - vMin) * 0.12;
  vMin -= padY;
  vMax += padY;

  const xAt = (i: number, t: number): number => {
    if (useTime) {
      return PX0 + ((t - tMin) / (tMax - tMin)) * (PX1 - PX0);
    }
    return n > 1 ? PX0 + (i / (n - 1)) * (PX1 - PX0) : (PX0 + PX1) / 2;
  };
  const yAt = (v: number): number => PY1 - ((v - vMin) / (vMax - vMin)) * (PY1 - PY0);

  const coords = parsed.map((d, i) => ({
    x: xAt(i, d.t),
    y: yAt(d.v),
    sev: severityOf(d.point, range),
    point: d.point,
  }));

  const polyPoints = coords.map((c) => `${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
  const lastCoord = coords[coords.length - 1];

  const unitSuffix = unit !== '' ? ` ${unit}` : '';
  const direction = last.v > first.v ? 'rising' : last.v < first.v ? 'falling' : 'unchanged';
  const summary = `${metric}: ${first.point.value} → ${last.point.value}${unitSuffix}, ${direction}`;

  let span = '';
  if (useTime) {
    const hours = (tMax - tMin) / 3_600_000;
    span = hours >= 48 ? ` over ${Math.round(hours / 24)} d` : ` over ${Math.round(hours)} h`;
  }
  const caption = `${first.point.value} → ${last.point.value}${unitSuffix}${span} · ${direction}`;
  const headUnit =
    range !== null
      ? `${unit}${unit !== '' ? ' · ' : ''}ref ${numLabel(range.low)}–${numLabel(range.high)}`
      : unit;

  // Reference band + y ticks. With a range we frame the normal zone; without,
  // we label the data extremes.
  const bandTop = range !== null ? yAt(range.high) : 0;
  const bandBottom = range !== null ? yAt(range.low) : 0;
  const yTicks =
    range !== null
      ? [
          { v: range.high, y: bandTop },
          { v: range.low, y: bandBottom },
        ]
      : [
          { v: Math.max(...parsed.map((d) => d.v)), y: yAt(Math.max(...parsed.map((d) => d.v))) },
          { v: Math.min(...parsed.map((d) => d.v)), y: yAt(Math.min(...parsed.map((d) => d.v))) },
        ];

  return (
    <figure className="metric-chart">
      <figcaption className="metric-head">
        <span className="metric-title">{metric}</span>
        {headUnit !== '' ? <span className="metric-unit">{headUnit}</span> : null}
      </figcaption>

      <svg
        className="metric-svg"
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label={summary}
      >
        <title>{summary}</title>

        {range !== null ? (
          <>
            <rect
              className="metric-band"
              x={PX0}
              y={bandTop}
              width={PX1 - PX0}
              height={Math.max(0, bandBottom - bandTop)}
            />
            <line className="metric-refline" x1={PX0} y1={bandTop} x2={PX1} y2={bandTop} />
            <line className="metric-refline" x1={PX0} y1={bandBottom} x2={PX1} y2={bandBottom} />
          </>
        ) : null}

        {/* baseline axis */}
        <line className="metric-axis" x1={PX0} y1={PY1} x2={PX1} y2={PY1} />

        {yTicks.map((tick) => (
          <text
            key={`y-${tick.v}`}
            className="metric-tick"
            x={PX0 - 6}
            y={tick.y}
            textAnchor="end"
            dominantBaseline="middle"
          >
            {numLabel(tick.v)}
          </text>
        ))}

        {n > 1 ? <polyline className="metric-line" pathLength={1} points={polyPoints} /> : null}

        {coords.map((c, i) => (
          <circle key={`pt-${i}`} className={pointClass(c.sev)} cx={c.x} cy={c.y} r={3.6} />
        ))}

        {lastCoord !== undefined ? (
          <text
            className="metric-endlabel"
            x={lastCoord.x + 7}
            y={lastCoord.y}
            textAnchor="start"
            dominantBaseline="middle"
          >
            {last.point.value}
          </text>
        ) : null}

        {/* sparse x ticks: first and last */}
        <text className="metric-tick" x={first !== last ? PX0 : (PX0 + PX1) / 2} y={H - 8} textAnchor="start">
          {fmtStamp(first.point.timestamp)}
        </text>
        {first !== last ? (
          <text className="metric-tick" x={PX1} y={H - 8} textAnchor="end">
            {fmtStamp(last.point.timestamp)}
          </text>
        ) : null}
      </svg>

      <p className="metric-caption">{caption}</p>

      <table className="visually-hidden">
        <caption>{summary}</caption>
        <thead>
          <tr>
            <th scope="col">Time</th>
            <th scope="col">Value{unit !== '' ? ` (${unit})` : ''}</th>
            <th scope="col">Flag</th>
          </tr>
        </thead>
        <tbody>
          {series.points.map((point, i) => (
            <tr key={`${point.resource_id}-${i}`}>
              <td>{fmtStamp(point.timestamp)}</td>
              <td>{point.value}</td>
              <td>{point.abnormal.trim() === '' ? 'normal' : point.abnormal}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
