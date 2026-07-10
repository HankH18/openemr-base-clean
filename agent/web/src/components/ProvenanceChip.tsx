import { Button, Dialog, DialogTrigger, Popover } from 'react-aria-components';
import type { SourceRef } from '../api/types';
import { humanizeLabel } from '../labels';

/**
 * The citation chip. Every claim carries one; pressing it opens the exact
 * (resource, value) pair the claim was extracted from. The inline chip shows
 * a friendly, doctor-useful source label — never the raw record UUID.
 */
export function ProvenanceChip({ source }: { source: SourceRef }): JSX.Element {
  const label = humanizeLabel(source.resource_type);
  return (
    <DialogTrigger>
      <Button className="prov-chip" aria-label={`Source: ${label}`}>
        <span className="prov-chip-check" aria-hidden="true">
          ✓
        </span>
        <span className="prov-chip-type">{label}</span>
      </Button>
      <Popover className="prov-pop" placement="bottom end" offset={6}>
        <Dialog className="prov-pop-dialog" aria-label="Source record">
          <dl className="prov-meta">
            <div>
              <dt>Resource</dt>
              <dd>{label}</dd>
            </div>
            <div>
              <dt>Recorded value</dt>
              <dd className="prov-value">{source.value}</dd>
            </div>
          </dl>
          <p className="prov-note">Quoted verbatim from the source record.</p>
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
