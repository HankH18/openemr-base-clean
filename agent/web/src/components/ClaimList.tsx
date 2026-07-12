import { useCallback, useState, type ReactNode } from 'react';
import { Button, Dialog, DialogTrigger, Popover } from 'react-aria-components';
import type {
  Claim,
  ClaimSeverity,
  ObservationSeries,
  TrendDirection,
  ValueDirection,
} from '../api/types';
import { claimTone, type ClaimTone } from '../fmt';
import { humanizeLabel, isNumericObservation, observationMetric, writableMetric } from '../labels';
import { EditRecordDialog, type ConfirmWrite, type ProposeWrite } from './EditRecordDialog';
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

/**
 * Severity → the metric-name colour class, a muted green→amber→red satisfaction
 * scale: normal is satisfactory (green), warning caution (amber), critical
 * unsatisfactory (red). Absent severity (non-observation claims) → default ink.
 */
function labelSeverityClass(severity: ClaimSeverity | null | undefined): string {
  if (severity === 'normal') {
    return ' claim-label--normal';
  }
  if (severity === 'warning') {
    return ' claim-label--warning';
  }
  if (severity === 'critical') {
    return ' claim-label--critical';
  }
  return '';
}

/**
 * Colour class for the movement arrow, from the grounded range-relative trend:
 * green moving toward the range ("improving"), red moving away ("worsening"),
 * and a muted neutral otherwise (steady / no range / no prior). Colour always
 * ships paired with the glyph (secondary encoding), so it is never sole signal.
 */
function arrowToneClass(direction: TrendDirection | null | undefined): string {
  if (direction === 'improving') {
    return ' claim-trend--improving';
  }
  if (direction === 'worsening') {
    return ' claim-trend--worsening';
  }
  return '';
}

const DIRECTION_GLYPH: Record<ValueDirection, string> = { up: '↑', down: '↓', none: '—' };
const DIRECTION_LABEL: Record<ValueDirection, string> = {
  up: 'value increased since prior reading',
  down: 'value decreased since prior reading',
  none: 'no change since prior reading',
};

/**
 * The uniform movement marker rendered from the structured fields (never parsed
 * from the text glyph): ↑ when the value rose, ↓ when it fell, — when unchanged
 * or there is no prior reading. Its colour comes from `trend_direction` (toward
 * the range → green, away → red, else neutral). Returns null when the claim
 * carries no value-direction (non-observation claims), so meds/conditions get
 * no marker. Leading space is bundled in so it sits cleanly after the value.
 */
function movementArrow(claim: Claim): ReactNode {
  const direction = claim.value_direction;
  if (direction !== 'up' && direction !== 'down' && direction !== 'none') {
    return null;
  }
  return (
    <>
      {' '}
      <span
        className={`claim-trend${arrowToneClass(claim.trend_direction)}`}
        role="img"
        aria-label={DIRECTION_LABEL[direction]}
      >
        {DIRECTION_GLYPH[direction]}
      </span>
    </>
  );
}

// The backend appends a trend suffix to observation claims: a change-indicator
// (↑N / ↓N / "→ no change" / "updated") optionally followed by a "· Nh since
// prior" recency tail. The structured movement arrow is now the sole directional
// signal, so we DROP the change-indicator glyph and KEEP the recency tail.
const TREND_SUFFIX_RE = /\s+(?:[↑↓]\S+|→ no change|·\s*updated)(\s*·\s*\S+\s+since prior)?\s*$/u;

/** Split a claim's post-label remainder into the value+unit `head` (arrow drawn
 *  after it) and the recency `tail` (kept verbatim), dropping the legacy glyph. */
function splitTrendSuffix(rest: string): { head: string; tail: string } {
  const match = TREND_SUFFIX_RE.exec(rest);
  if (match === null || match.index === undefined) {
    return { head: rest, tail: '' };
  }
  return { head: rest.slice(0, match.index), tail: match[1] ?? '' };
}

/**
 * Render a claim as "Label: value <arrow> …" — a humanized label coloured by the
 * grounded severity scale, the recorded value bolded, then the uniform movement
 * arrow (↑/↓/—) coloured by the grounded trend, then the kept recency tail. The
 * legacy delta glyph in the text is dropped in favour of the structured arrow.
 * Prose claims (a long or absent leading label) render as-is with the value
 * bolded. Tone is derived from the ORIGINAL text so it is unaffected by the split.
 */
function claimText(claim: Claim): ReactNode {
  const { text, source_ref, severity } = claim;
  const value = source_ref.value;
  const tone = claimTone(text);

  const sepIdx = text.indexOf(': ');
  if (sepIdx !== -1) {
    const label = text.slice(0, sepIdx);
    const rest = text.slice(sepIdx + 2);
    // A short leading segment is a label; anything longer is prose.
    if (label.trim().split(/\s+/).length <= 4) {
      const { head, tail } = splitTrendSuffix(rest);
      return (
        <>
          <span className={`claim-label${labelSeverityClass(severity)}`}>
            {`${humanizeLabel(label)}: `}
          </span>
          {withBoldedValue(head, value, tone)}
          {movementArrow(claim)}
          {tail}
        </>
      );
    }
  }

  return withBoldedValue(text, value, tone);
}

export function ClaimList({
  claims,
  dense = false,
  fetchTrend,
  proposeWrite,
  confirmWrite,
}: {
  claims: Claim[];
  dense?: boolean;
  /** When provided, numeric Observation claims gain a "View trend" chart. */
  fetchTrend?: FetchTrend;
  /**
   * When both are provided, claims whose metric is a writable vital gain an
   * "Edit" affordance (propose → echo-back → confirm). Bound to the current
   * patient + clinician at the App seam, exactly like `fetchTrend`.
   */
  proposeWrite?: ProposeWrite;
  confirmWrite?: ConfirmWrite;
}): JSX.Element {
  const canEdit = proposeWrite !== undefined && confirmWrite !== undefined;
  return (
    <ul className={dense ? 'claims claims--dense' : 'claims'}>
      {claims.map((claim, i) => {
        const editMetric = canEdit ? writableMetric(claim) : null;
        return (
          <li className="claim" key={`${claim.source_ref.resource_id}-${i}`}>
            <span className="claim-text">{claimText(claim)}</span>
            <span className="claim-tools">
              {fetchTrend !== undefined && isNumericObservation(claim) ? (
                <TrendChip claim={claim} fetchTrend={fetchTrend} />
              ) : null}
              {editMetric !== null && proposeWrite !== undefined && confirmWrite !== undefined ? (
                <EditRecordDialog
                  claim={claim}
                  metric={editMetric}
                  proposeWrite={proposeWrite}
                  confirmWrite={confirmWrite}
                />
              ) : null}
              <ProvenanceChip source={claim.source_ref} />
            </span>
          </li>
        );
      })}
    </ul>
  );
}
