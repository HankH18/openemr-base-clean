import type { GuidelineEvidenceItem } from '../api/types';

/** Human source line for one evidence item — id from the citation, else the row. */
function sourceLabel(item: GuidelineEvidenceItem): string {
  const id = item.citation?.source_id ?? item.document_id;
  return id !== '' ? `Guideline ${id}` : 'Guideline corpus';
}

/**
 * The SEPARATE guideline-evidence block under a chat answer. Guideline
 * backing is literature, never a patient observation — so it renders as its
 * own clearly-labeled section, visually distinct from the claim rows and
 * their provenance chips (dotted frame + section-sign kicker, the same motif
 * as the guideline citation chip). Each item shows its section, the
 * retrieved passage, and its source. Empty evidence renders nothing.
 */
export function GuidelineEvidenceBlock({
  items,
}: {
  items: GuidelineEvidenceItem[];
}): JSX.Element | null {
  if (items.length === 0) {
    return null;
  }
  return (
    <section className="guideline-block" aria-label="Guideline evidence">
      <h3 className="guideline-kicker">
        <span className="guideline-glyph" aria-hidden="true">
          §
        </span>
        Guideline evidence
      </h3>
      <ul className="guideline-list">
        {items.map((item, index) => (
          <li className="guideline-item" key={item.chunk_id !== '' ? item.chunk_id : index}>
            {item.section !== '' ? <p className="guideline-section">{item.section}</p> : null}
            <blockquote className="guideline-quote">{item.content}</blockquote>
            <p className="guideline-source">{sourceLabel(item)}</p>
          </li>
        ))}
      </ul>
      <p className="guideline-note">
        Supporting literature — not this patient&rsquo;s record.
      </p>
    </section>
  );
}
