import { useCallback, useState, type FormEvent } from 'react';
import { Button, Dialog, DialogTrigger, Input, Popover, TextField } from 'react-aria-components';
import type { Claim, CommittedWrite, ProposedWrite, WriteCandidate, WriteKind } from '../api/types';
import { WriteDisabledError, WriteRejectedError } from '../api/types';
import { fmtStamp } from '../fmt';
import { WRITABLE_METRICS, type WritableMetric } from '../labels';

/**
 * Propose a human-typed vital edit. Bound to the current patient + clinician at
 * the App seam, so the dialog only supplies the metric/value/unit.
 */
export type ProposeWrite = (
  kind: WriteKind,
  metric: string,
  rawValue: string,
  unit: string,
) => Promise<ProposedWrite>;

/** Commit the reviewed candidate verbatim, keyed by its idempotency key. */
export type ConfirmWrite = (
  candidate: WriteCandidate,
  idempotencyKey: string,
) => Promise<CommittedWrite>;

/**
 * The dialog's step in the propose → review → confirm gate. `reviewing` and
 * `saving` are the in-flight variants of `edit` and `review`; `disabled` is the
 * 503 terminal state; `saved` is the committed terminal state.
 */
type Phase = 'edit' | 'reviewing' | 'review' | 'saving' | 'saved' | 'disabled';

function errorList(errors: string[], key: string): JSX.Element {
  return (
    <div className="edit-errors" role="alert">
      {errors.map((message, i) => (
        <p key={`${key}-${i}`}>{message}</p>
      ))}
    </div>
  );
}

/**
 * Physician direct-edit of a writable vital, in a React-Aria
 * DialogTrigger/Popover mirroring ProvenanceChip/TrendChip. The unit is LOCKED
 * and the metric is a fixed label — only a numeric value is typed. Step 1
 * (Review) proposes the write and renders the server's structured echo-back;
 * step 2 (Confirm & save) commits it as a NEW dated record. Append-only: no
 * prior value is overwritten. The 503 "disabled" and 400 "bad value" cases are
 * surfaced in place rather than thrown at the user.
 */
export function EditRecordDialog({
  claim,
  metric,
  proposeWrite,
  confirmWrite,
}: {
  claim: Claim;
  metric: WritableMetric;
  proposeWrite: ProposeWrite;
  confirmWrite: ConfirmWrite;
}): JSX.Element {
  const spec = WRITABLE_METRICS[metric];
  const current = claim.source_ref.value.trim();

  const [phase, setPhase] = useState<Phase>('edit');
  const [draft, setDraft] = useState(current);
  const [proposed, setProposed] = useState<ProposedWrite | null>(null);
  const [committed, setCommitted] = useState<CommittedWrite | null>(null);
  const [errors, setErrors] = useState<string[]>([]);

  const reset = useCallback(() => {
    setPhase('edit');
    setDraft(current);
    setProposed(null);
    setCommitted(null);
    setErrors([]);
  }, [current]);

  const runPropose = useCallback(() => {
    const raw = draft.trim();
    if (raw === '') {
      return;
    }
    setErrors([]);
    setPhase('reviewing');
    proposeWrite('vital', spec.metric, raw, spec.unit)
      .then((result) => {
        setProposed(result);
        // A hard block can also arrive on a 200 with blocked=true — honor it.
        if (result.verdict.blocked || result.verdict.errors.length > 0) {
          setErrors(
            result.verdict.errors.length > 0
              ? result.verdict.errors
              : ['That value cannot be recorded.'],
          );
          setPhase('edit');
          return;
        }
        setPhase('review');
      })
      .catch((err: unknown) => {
        if (err instanceof WriteDisabledError) {
          setPhase('disabled');
          return;
        }
        if (err instanceof WriteRejectedError) {
          setErrors(err.errors);
          setPhase('edit');
          return;
        }
        setErrors(['Could not reach the record service. Nothing was changed.']);
        setPhase('edit');
      });
  }, [draft, proposeWrite, spec.metric, spec.unit]);

  const runConfirm = useCallback(() => {
    if (proposed === null) {
      return;
    }
    const candidate = proposed.candidate;
    setErrors([]);
    setPhase('saving');
    confirmWrite(candidate, candidate.idempotency_key)
      .then((result) => {
        setCommitted(result);
        setPhase('saved');
      })
      .catch((err: unknown) => {
        if (err instanceof WriteDisabledError) {
          setPhase('disabled');
          return;
        }
        if (err instanceof WriteRejectedError) {
          setErrors(err.errors);
          setPhase('review');
          return;
        }
        setErrors(['The record service did not confirm the write — nothing was saved.']);
        setPhase('review');
      });
  }, [proposed, confirmWrite]);

  const onSubmit = useCallback(
    (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      runPropose();
    },
    [runPropose],
  );

  // The value/unit the physician confirms against is the server's echo-back,
  // not the raw draft — that is the whole point of the review step.
  const echoValue = proposed?.candidate.vital?.value ?? draft.trim();
  const echoUnit = proposed?.candidate.vital?.unit ?? spec.unit;
  const reviewing = phase === 'reviewing';
  const saving = phase === 'saving';

  return (
    <DialogTrigger
      onOpenChange={(open) => {
        if (open) {
          reset();
        }
      }}
    >
      <Button className="edit-chip" aria-label={`Edit ${spec.label}`}>
        <svg className="edit-chip-icon" viewBox="0 0 14 14" aria-hidden="true">
          <path d="M9.4 2.1 11.9 4.6 5 11.5 2 12l.5-3z" />
          <path d="M8.4 3.1 10.9 5.6" />
        </svg>
        <span className="edit-chip-text">Edit</span>
      </Button>
      <Popover className="edit-pop" placement="bottom end" offset={6}>
        <Dialog className="edit-pop-dialog" aria-label={`Edit ${spec.label}`}>
          {({ close }) => {
            if (phase === 'disabled') {
              return (
                <>
                  <div className="edit-head">
                    <span className="edit-kicker edit-kicker--muted">Unavailable</span>
                    <h3 className="edit-title">Direct edit is off</h3>
                  </div>
                  <p className="edit-prose">
                    Record write-back is disabled on this deployment, so no change can be
                    saved. Nothing was written.
                  </p>
                  <div className="edit-actions">
                    <Button className="btn btn--quiet" onPress={close}>
                      Close
                    </Button>
                  </div>
                </>
              );
            }

            if (phase === 'saved') {
              return (
                <>
                  <div className="edit-head">
                    <span className="edit-kicker edit-kicker--ok">Saved</span>
                    <h3 className="edit-title">{spec.label} recorded</h3>
                  </div>
                  <p className="edit-prose">
                    A new {spec.label.toLowerCase()} of{' '}
                    <strong className="edit-strong">
                      {echoValue} {echoUnit}
                    </strong>{' '}
                    was appended — prior values are unchanged.
                  </p>
                  <dl className="edit-meta">
                    {committed !== null && committed.new_id !== '' ? (
                      <div>
                        <dt>Record</dt>
                        <dd>{committed.new_id}</dd>
                      </div>
                    ) : null}
                    {committed !== null && committed.committed_at !== '' ? (
                      <div>
                        <dt>Committed</dt>
                        <dd>{fmtStamp(committed.committed_at)}</dd>
                      </div>
                    ) : null}
                  </dl>
                  <div className="edit-actions">
                    <Button className="btn btn--primary" onPress={close}>
                      Done
                    </Button>
                  </div>
                </>
              );
            }

            if (phase === 'review' || phase === 'saving') {
              return (
                <>
                  <div className="edit-head">
                    <span className="edit-kicker">Confirm new record</span>
                    <h3 className="edit-title">{spec.label}</h3>
                  </div>
                  <div className="edit-echo">
                    <span className="edit-echo-from">
                      {current} {spec.unit}
                    </span>
                    <span className="edit-echo-arrow" aria-hidden="true">
                      →
                    </span>
                    <span className="edit-echo-to">
                      {echoValue} {echoUnit}
                    </span>
                  </div>
                  {proposed !== null && proposed.verdict.warnings.length > 0 ? (
                    <div className="edit-warn" role="alert">
                      {proposed.verdict.warnings.map((warning, i) => (
                        <p key={`warn-${i}`}>{warning}</p>
                      ))}
                    </div>
                  ) : null}
                  <p className="edit-notice">
                    {proposed?.notice !== undefined && proposed.notice !== ''
                      ? proposed.notice
                      : 'This creates a NEW record dated now; it does not overwrite prior values.'}
                  </p>
                  {errors.length > 0 ? errorList(errors, 'confirm') : null}
                  <div className="edit-actions">
                    <Button
                      className="btn btn--quiet"
                      onPress={() => {
                        setErrors([]);
                        setPhase('edit');
                      }}
                      isDisabled={saving}
                    >
                      Back
                    </Button>
                    <Button className="btn btn--primary" onPress={runConfirm} isDisabled={saving}>
                      {saving ? 'Saving…' : 'Confirm & save'}
                    </Button>
                  </div>
                </>
              );
            }

            // 'edit' / 'reviewing'
            return (
              <form className="edit-form" onSubmit={onSubmit}>
                <div className="edit-head">
                  <span className="edit-kicker">Direct edit</span>
                  <h3 className="edit-title">{spec.label}</h3>
                </div>
                <p className="edit-sub">
                  Correct or update this reading. You&rsquo;ll review it before it saves as a
                  new dated record.
                </p>
                <dl className="edit-meta">
                  <div>
                    <dt>On file</dt>
                    <dd>
                      {current} {spec.unit}
                    </dd>
                  </div>
                </dl>
                <div className="edit-field-row">
                  <TextField
                    className="edit-field"
                    aria-label={`New ${spec.label} value`}
                    value={draft}
                    onChange={setDraft}
                    isDisabled={reviewing}
                    autoFocus
                  >
                    <Input
                      className="edit-input"
                      inputMode="decimal"
                      placeholder="New value"
                    />
                  </TextField>
                  <span className="edit-unit" aria-label={`Unit ${spec.unit}, locked`}>
                    <svg className="edit-unit-lock" viewBox="0 0 10 12" aria-hidden="true">
                      <rect x="1.5" y="5" width="7" height="6" rx="1" />
                      <path d="M3 5V3.5a2 2 0 0 1 4 0V5" />
                    </svg>
                    {spec.unit}
                  </span>
                </div>
                {errors.length > 0 ? errorList(errors, 'edit') : null}
                <div className="edit-actions">
                  <Button className="btn btn--quiet" onPress={close} isDisabled={reviewing}>
                    Cancel
                  </Button>
                  <Button
                    type="submit"
                    className="btn btn--primary"
                    isDisabled={reviewing || draft.trim() === ''}
                  >
                    {reviewing ? 'Checking…' : 'Review change'}
                  </Button>
                </div>
              </form>
            );
          }}
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
