import { useCallback, useState, type ReactNode } from 'react';
import { Button, Dialog, DialogTrigger, Popover } from 'react-aria-components';
import type { Claim, ClaimSeverity, ObservationSeries, TrendDirection } from '../api/types';
import { claimTone, type ClaimTone } from '../fmt';
import { humanizeLabel, isNumericObservation, observationMetric } from '../labels';
import { MetricChart } from './MetricChart';
import { ProvenanceChip } from './ProvenanceChip';

type FetchTrend = (metric: string) => Promise<ObservationSeries>;
type TrendState = 'idle' | 'loading' | 'ready' | 'error';

/**
 * "View trend" affordance for a numeric Observation claim. Mirrors the
 * ProvenanceChip DialogTrigger/Popover pattern; the series is fetched lazily
 * the first time the popover opens, then rendered as a MetricChart.
 */
function TrendChip({ claim, fetchTrend }: { claim: Claim; fetchTrend: FetchTrend }): JSX.Element {
  const metric = observationMetric(claim);
  const [state, setState] = useState<TrendState>('idle');
  const [series, setSeries] = useState<ObservationSeries | null>(null);

  const load = useCallback(() => {
    setState('loading');
    fetchTrend(metric)
      .then((result) => {
        setSeries(result);
        setState('ready');
      })
      .catch(() => {
        setState('error');
      });
  }, [fetchTrend, metric]);

  return (
    <DialogTrigger
      onOpenChange={(open) => {
        if (open && state === 'idle') {
          load();
        }
      }}
    >
      <Button className="trend-chip" aria-label={`View ${metric} trend`}>
        <svg className="trend-chip-spark" viewBox="0 0 12 8" aria-hidden="true">
          <polyline points="0,7 3,5 6,6 9,1 12,2" />
        </svg>
        <span className="trend-chip-text">Trend</span>
      </Button>
      <Popover className="trend-pop" placement="bottom end" offset={6}>
        <Dialog className="trend-pop-dialog" aria-label={`${metric} trend`}>
          {state === 'ready' && series !== null ? (
            series.points.length > 0 ? (
              <MetricChart series={series} />
            ) : (
              <p className="trend-empty">No serial readings on file for {metric}.</p>
            )
          ) : state === 'error' ? (
            <p className="trend-empty">Trend unavailable right now.</p>
          ) : (
            <p className="trend-loading">Loading {metric} trend…</p>
          )}
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}

/**
 * Bold the recorded value inside a text segment, toned by severity. Returns
 * the segment untouched when the value cannot be located within it.
 */
function withBoldedValue(segment: string, value: string, tone: ClaimTone | null): ReactNode {
  const idx = value.length > 0 ? segment.indexOf(value) : -1;
  if (idx === -1) {
    return segment;
  }
  const toneClass = tone !== null ? ` claim-val--${tone}` : '';
  return (
    <>
      {segment.slice(0, idx)}
      <strong className={`claim-val${toneClass}`}>{value}</strong>
      {segment.slice(idx + value.length)}
    </>
  );
}

/** Severity → the metric-label colour class (normal/absent → default ink). */
function labelSeverityClass(severity: ClaimSeverity | null | undefined): string {
  if (severity === 'critical') {
    return ' claim-label--critical';
  }
  if (severity === 'warning') {
    return ' claim-label--warning';
  }
  return '';
}

/**
 * Trend-arrow colour class from the grounded direction. "improving" gets the
 * distinct positive token; "worsening" reuses the reserved status hue matching
 * the claim's severity (critical vs warning); "steady"/absent stays neutral so
 * an in-range fluctuation is not dressed up as a directional signal.
 */
function trendArrowClass(
  direction: TrendDirection | null | undefined,
  severity: ClaimSeverity | null | undefined,
): string | null {
  if (direction === 'improving') {
    return 'claim-trend--improving';
  }
  if (direction === 'worsening') {
    return severity === 'critical' ? 'claim-trend--critical' : 'claim-trend--warning';
  }
  return null;
}

// The trend arrow plus its delta run, e.g. "↓12" / "↑0.5" — a rising or falling
// glyph followed by its (space-terminated) magnitude. A "→ no change" suffix has
// no directional glyph and is left in default ink.
const TREND_ARROW_RE = /[↑↓]\S*/u;

/**
 * Render the post-label remainder: bold the recorded value (toned by the legacy
 * text heuristic) and, when the trend is directional, colour the ↑/↓ glyph by
 * `trendClass`. The value always precedes the trend suffix, so the pre-arrow
 * slice is where the value lives.
 */
function withValueAndTrend(
  segment: string,
  value: string,
  tone: ClaimTone | null,
  trendClass: string | null,
): ReactNode {
  if (trendClass === null) {
    return withBoldedValue(segment, value, tone);
  }
  const match = TREND_ARROW_RE.exec(segment);
  if (match === null || match.index === undefined) {
    return withBoldedValue(segment, value, tone);
  }
  const before = segment.slice(0, match.index);
  const arrow = match[0];
  const after = segment.slice(match.index + arrow.length);
  return (
    <>
      {withBoldedValue(before, value, tone)}
      <span className={`claim-trend ${trendClass}`}>{arrow}</span>
      {after}
    </>
  );
}

/**
 * Render a claim as "Label: value …" — a humanized label (coloured by the
 * grounded severity) followed by the remainder, with the recorded value bolded
 * and the trend arrow coloured by the grounded direction. Prose claims (a long
 * or absent leading label) are rendered as-is with the value bolded. Tone is
 * always derived from the ORIGINAL text so coloring is unaffected by the split.
 */
function claimText(claim: Claim): ReactNode {
  const { text, source_ref, severity, trend_direction } = claim;
  const value = source_ref.value;
  const tone = claimTone(text);
  const trendClass = trendArrowClass(trend_direction, severity);

  const sepIdx = text.indexOf(': ');
  if (sepIdx !== -1) {
    const label = text.slice(0, sepIdx);
    const rest = text.slice(sepIdx + 2);
    // A short leading segment is a label; anything longer is prose.
    if (label.trim().split(/\s+/).length <= 4) {
      return (
        <>
          <span className={`claim-label${labelSeverityClass(severity)}`}>
            {`${humanizeLabel(label)}: `}
          </span>
          {withValueAndTrend(rest, value, tone, trendClass)}
        </>
      );
    }
  }

  return withValueAndTrend(text, value, tone, trendClass);
}

export function ClaimList({
  claims,
  dense = false,
  fetchTrend,
}: {
  claims: Claim[];
  dense?: boolean;
  /** When provided, numeric Observation claims gain a "View trend" chart. */
  fetchTrend?: FetchTrend;
}): JSX.Element {
  return (
    <ul className={dense ? 'claims claims--dense' : 'claims'}>
      {claims.map((claim, i) => (
        <li className="claim" key={`${claim.source_ref.resource_id}-${i}`}>
          <span className="claim-text">{claimText(claim)}</span>
          <span className="claim-tools">
            {fetchTrend !== undefined && isNumericObservation(claim) ? (
              <TrendChip claim={claim} fetchTrend={fetchTrend} />
            ) : null}
            <ProvenanceChip source={claim.source_ref} />
          </span>
        </li>
      ))}
    </ul>
  );
}
