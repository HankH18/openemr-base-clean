import { Button, Dialog, DialogTrigger, Popover } from 'react-aria-components';
import { documentPageUrl } from '../api/documents';
import type { Citation, SourceRef } from '../api/types';
import { adaptCitation } from '../citations';
import { DocumentEvidence } from './DocumentEvidence';

/**
 * The citation chip. Every claim carries one; pressing it opens the exact
 * source the claim was extracted from. The inline chip shows a friendly,
 * doctor-useful source label — never a raw record UUID.
 *
 * Week 2: the chip renders every variant of the citation union — a record
 * citation (fhir, the Week 1 default), a document citation (uploaded page +
 * bbox), and a guideline citation — each visually distinct via
 * `data-variant` / `.prov-chip--<variant>`, plus a fail-safe `unknown`
 * fallback for citation types this build does not recognize. All shaping
 * lives in the adapter (src/citations.ts); this component just renders the
 * model.
 *
 * Document citations additionally render the cited page image with the
 * extracted region boxed (`DocumentEvidence` → `EvidenceOverlay`), so the
 * physician sees exactly where the quoted value came from. The text rows
 * stay alongside the visual — and remain the whole popover whenever the
 * page image is unavailable.
 */
export function ProvenanceChip({ source }: { source: SourceRef | Citation }): JSX.Element {
  const model = adaptCitation(source);
  // The page-evidence visual needs all three coordinates of the citation:
  // which document, which page, and where on the page. Any missing piece
  // (or a page image that fails to load) degrades to the text rows.
  const evidence =
    model.variant === 'document' &&
    model.sourceId !== null &&
    model.pageNumber !== null &&
    model.bbox !== null
      ? {
          src: documentPageUrl(model.sourceId, model.pageNumber),
          alt: `Uploaded document ${model.sourceId}, page ${model.pageNumber} — cited region highlighted`,
          boxes: [{ bbox: model.bbox, label: model.quote ?? undefined }],
        }
      : null;
  return (
    <DialogTrigger>
      <Button
        className={`prov-chip prov-chip--${model.variant}`}
        data-variant={model.variant}
        aria-label={`Source: ${model.chipLabel}`}
      >
        <span className="prov-chip-check" aria-hidden="true">
          {model.glyph}
        </span>
        <span className="prov-chip-type">{model.chipLabel}</span>
      </Button>
      <Popover className="prov-pop" placement="bottom end" offset={6}>
        <Dialog
          className={`prov-pop-dialog${evidence !== null ? ' prov-pop-dialog--evidence' : ''}`}
          aria-label="Cited source"
        >
          {evidence !== null ? (
            <DocumentEvidence src={evidence.src} alt={evidence.alt} boxes={evidence.boxes} />
          ) : null}
          <dl className="prov-meta">
            {model.details.map((detail) => (
              <div key={detail.term}>
                <dt>{detail.term}</dt>
                <dd className={detail.emphasis ? 'prov-value' : undefined}>{detail.value}</dd>
              </div>
            ))}
          </dl>
          <p className="prov-note">{model.note}</p>
        </Dialog>
      </Popover>
    </DialogTrigger>
  );
}
