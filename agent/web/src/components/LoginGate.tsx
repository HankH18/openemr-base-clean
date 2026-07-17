import type { ReactNode } from 'react';
import { Button } from 'react-aria-components';
import type { AuthState } from '../state/useAuth';
import { redirectToLogin } from '../api/session';

/**
 * The "Sign in with OpenEMR" gate. Active ONLY when the backend reports SMART
 * auth: in auth-disabled and mock/offline mode it renders its children
 * unchanged, so the current no-login demo is byte-for-byte what it is today.
 *
 * - disabled / mock, or already authenticated → render the app (children).
 * - SMART + probing → a small branded loading screen.
 * - SMART + not authenticated → the sign-in screen.
 */

function Brand(): JSX.Element {
  return (
    <header className="topbar">
      <div className="wordmark">
        <span className="wordmark-name">Rounds</span>
        <span className="wordmark-sub">Clinical Co-Pilot</span>
      </div>
    </header>
  );
}

export function LoginGate({
  auth,
  children,
}: {
  auth: AuthState;
  children: ReactNode;
}): JSX.Element {
  // The only path the current demo ever takes.
  if (auth.authMode === 'disabled' || auth.authenticated) {
    return <>{children}</>;
  }

  if (auth.status === 'loading') {
    return (
      <div className="app">
        <Brand />
        <div className="gate">
          <div className="gate-pulse" aria-hidden="true" />
          <h1 className="gate-title">Checking your session.</h1>
        </div>
      </div>
    );
  }

  return (
    <div className="app">
      <Brand />
      <div className="gate">
        <div className="gate-pulse" aria-hidden="true" />
        <h1 className="gate-title">Sign in to start rounding.</h1>
        <p className="gate-sub">
          Rounds reads from the live OpenEMR record. Sign in with your OpenEMR account to load
          your list — every card stays grounded in the chart before it reaches you.
        </p>
        <Button className="btn btn--primary" onPress={redirectToLogin}>
          Sign in with OpenEMR
        </Button>
      </div>
    </div>
  );
}
