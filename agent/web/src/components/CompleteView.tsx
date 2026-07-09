import { Button } from 'react-aria-components';
import { patientName } from '../census';

export function CompleteView({
  seen,
  onRestart,
}: {
  seen: number[];
  onRestart: () => void;
}): JSX.Element {
  return (
    <div className="gate">
      <h1 className="gate-title">Rounds complete.</h1>
      <p className="gate-sub">
        {seen.length} {seen.length === 1 ? 'patient' : 'patients'} seen, sickest first. The queue
        re-ranks from live records whenever you start another pass.
      </p>
      <ol className="done-list">
        {seen.map((id, index) => (
          <li key={id} className="done-row">
            <span className="q-rank">{String(index + 1).padStart(2, '0')}</span>
            <span className="done-name">{patientName(id)}</span>
            <span className="q-status q-status--seen">Seen</span>
          </li>
        ))}
      </ol>
      <Button className="btn btn--primary" onPress={onRestart}>
        Start another pass
      </Button>
    </div>
  );
}
