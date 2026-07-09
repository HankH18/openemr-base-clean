import { Button, Dialog, DialogTrigger, Popover } from 'react-aria-components';
import type { SourceRef } from '../api/types';

/**
 * The citation chip. Every claim carries one; pressing it opens the exact
 * (resource, field, value) triple the claim was extracted from.
 */
export function ProvenanceChip({ source }: { source: SourceRef }): JSX.Element {
  return (
    <DialogTrigger>
      <Button
        className="prov-chip"
        aria-label={`Source: ${source.resource_type} ${source.resource_id}`}
      >
        <span className="prov-chip-type">{source.resource_type}</span>
        <span className="prov-chip-id">{source.resource_id}</span>
      </Button>
      <Popover className="prov-pop" placement="bottom end" offset={6}>
        <Dialog className="prov-pop-dialog" aria-label="Source record">
          <dl className="prov-meta">
            <div>
              <dt>Resource</dt>
              <dd>{source.resource_type}</dd>
            </div>
            <div>
              <dt>Record id</dt>
              <dd>{source.resource_id}</dd>
            </div>
            <div>
              <dt>Field</dt>
              <dd>{source.field}</dd>
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
