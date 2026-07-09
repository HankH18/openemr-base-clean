import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Button, Input, TextField } from 'react-aria-components';
import type { Verification } from '../api/types';
import type { ChatMessage } from '../state/useChat';
import { ClaimList } from './ClaimList';

const VERIFICATION_LABEL: Record<Verification['action'], string> = {
  served: 'Verified — served',
  degraded: 'Degraded — re-check incomplete',
  withheld: 'Withheld — no source found',
};

function answerClass(message: ChatMessage): string {
  if (message.pending) {
    return 'msg msg--a msg--pending';
  }
  const action = message.verification?.action ?? 'served';
  return `msg msg--a msg--${action}`;
}

function Answer({ message }: { message: ChatMessage }): JSX.Element {
  const action = message.verification?.action;
  return (
    <div className={answerClass(message)}>
      {message.pending ? (
        <div className="v-row v-row--pending">
          <span className="v-dot" />
          Verifying against the record
        </div>
      ) : action !== undefined ? (
        <div className={`v-row v-row--${action}`}>
          <span className="v-dot" />
          {VERIFICATION_LABEL[action]}
        </div>
      ) : null}
      <p className={message.pending ? 'msg-a-body pending-pulse' : 'msg-a-body'}>{message.text}</p>
      {message.claims.length > 0 ? (
        <div className="msg-a-claims">
          <ClaimList claims={message.claims} dense />
        </div>
      ) : null}
      {message.correlationId !== null ? (
        <p className="msg-a-foot">corr {message.correlationId}</p>
      ) : null}
    </div>
  );
}

/**
 * Drill-down on the current patient. Answers are cited or withheld — the
 * verification outcome is rendered as form, not fine print.
 */
export function ChatPanel({
  given,
  messages,
  busy,
  suggestions,
  onSend,
}: {
  given: string;
  messages: ChatMessage[];
  busy: boolean;
  suggestions: string[];
  onSend: (message: string) => void;
}): JSX.Element {
  const [draft, setDraft] = useState('');
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const log = logRef.current;
    if (log !== null) {
      log.scrollTop = log.scrollHeight;
    }
  }, [messages]);

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const message = draft.trim();
    if (message === '' || busy) {
      return;
    }
    setDraft('');
    onSend(message);
  }

  return (
    <section className="chat" aria-label={`Ask the chart about ${given}`}>
      <div className="chat-head">
        <h2 className="chat-title">Ask the chart</h2>
        <span className="chat-sub">Cited from the record, or withheld — never guessed.</span>
      </div>
      <div className="chat-log" ref={logRef}>
        {messages.length === 0 ? (
          <p className="chat-empty">
            Drill into {given}&rsquo;s chart. Every answer cites the source record; if no source
            exists, the answer is withheld.
          </p>
        ) : null}
        {messages.map((message) =>
          message.kind === 'question' ? (
            <div className="msg msg--q" key={message.id}>
              <span className="msg-q-label">You</span>
              <p className="msg-q-text">{message.text}</p>
            </div>
          ) : message.kind === 'error' ? (
            <div className="msg msg--error" key={message.id} role="alert">
              {message.text}
            </div>
          ) : (
            <Answer message={message} key={message.id} />
          ),
        )}
      </div>
      {suggestions.length > 0 ? (
        <div className="chat-suggest">
          {suggestions.map((suggestion) => (
            <Button
              key={suggestion}
              className="suggest-chip"
              isDisabled={busy}
              onPress={() => {
                onSend(suggestion);
              }}
            >
              {suggestion}
            </Button>
          ))}
        </div>
      ) : null}
      <form className="chat-form" onSubmit={submit}>
        <TextField
          className="chat-field"
          aria-label={`Question about ${given}`}
          value={draft}
          onChange={setDraft}
          isDisabled={busy}
        >
          <Input className="chat-input" placeholder={`Ask about ${given}…`} />
        </TextField>
        <Button
          type="submit"
          className="btn btn--primary"
          isDisabled={busy || draft.trim() === ''}
        >
          Ask
        </Button>
      </form>
    </section>
  );
}
