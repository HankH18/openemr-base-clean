import { Button } from 'react-aria-components';
import { CLINICIAN_LABEL } from '../census';
import type { Theme } from '../state/theme';

export function TopBar({
  mode,
  theme,
  onToggleTheme,
  onRecheck,
  recheckStatus,
  rechecking,
  showRecheck,
  authenticated = false,
  displayName = null,
  onLogout,
}: {
  mode: 'mock' | 'live';
  theme: Theme;
  onToggleTheme: () => void;
  onRecheck: () => void;
  recheckStatus: string | null;
  rechecking: boolean;
  showRecheck: boolean;
  /** SMART auth signed-in state. Defaults keep the disabled/mock look unchanged. */
  authenticated?: boolean;
  displayName?: string | null;
  onLogout?: () => void;
}): JSX.Element {
  const signedIn = authenticated && displayName !== null;

  return (
    <header className="topbar">
      <div className="wordmark">
        <span className="wordmark-name">Rounds</span>
        <span className="wordmark-sub">Clinical Co-Pilot</span>
      </div>
      {mode === 'mock' ? <span className="mode-tag">Demo data</span> : null}
      <div className="topbar-spacer" />
      {recheckStatus !== null ? (
        <span className="recheck-status" role="status">
          {recheckStatus}
        </span>
      ) : null}
      {showRecheck ? (
        <Button className="btn" onPress={onRecheck} isDisabled={rechecking}>
          {rechecking ? 'Re-checking…' : 'Re-check charts'}
        </Button>
      ) : null}
      <Button
        className="btn btn--quiet"
        onPress={onToggleTheme}
        aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
      >
        {theme === 'dark' ? 'Light' : 'Dark'}
      </Button>
      {signedIn ? (
        <>
          <span className="clinician">{displayName}</span>
          <Button
            className="btn btn--quiet"
            onPress={() => {
              onLogout?.();
            }}
          >
            Logout
          </Button>
        </>
      ) : (
        <span className="clinician">{CLINICIAN_LABEL}</span>
      )}
    </header>
  );
}
