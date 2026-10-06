import { api } from '../lib/api';
import { banner, byId, h, replace } from '../lib/dom';
import type { Me } from '../lib/types';

const MESSAGES: Record<string, string> = {
  NOT_INVITED: 'This Google account has not been invited. Ask an administrator for an invitation, then open the invitation link.',
  INVITATION_INVALID: 'The invitation link is invalid, expired, or has already been used. Ask an administrator for a new invitation.',
  EMAIL_MISMATCH: 'You signed in with a different Google account than the one that was invited. Sign in with the invited email address.',
  EMAIL_UNVERIFIED: 'Google reports this email address as unverified, so it cannot be used to sign in.',
  ACCOUNT_REVOKED: 'Access for this account has been revoked. Contact an administrator if you think this is a mistake.',
  OAUTH_FAILED: 'Sign-in with Google did not complete. Please try again.',
  STATE_INVALID: 'The sign-in attempt expired or was started in another tab or browser. Please start again.',
  BOOTSTRAP_INVALID: 'The setup link is invalid or has already been used. The first administrator can only be set up once.',
};

const code = new URLSearchParams(location.search).get('error');
if (code) {
  const safeCode = /^[A-Z_]{1,40}$/.test(code) ? code : 'UNKNOWN';
  const msg = MESSAGES[safeCode] ?? 'Sign-in failed. Please try again.';
  replace(byId('login-error'), banner('bad', 'Sign-in failed.', msg, h('span', { class: 'small muted', text: ` (code ${safeCode})` })));
  // Keep the address bar clean so a reload does not show a stale error.
  history.replaceState(null, '', location.pathname);
}

// If a session already exists, offer a shortcut instead of a second sign-in.
api.get<Me>('/api/me', { noAuthRedirect: true }).then((me) => {
  replace(byId('login-session'), banner('ok', 'Already signed in', `as ${me.user.email}. `, h('a', { attrs: { href: '/' }, text: 'Continue to the overview' })));
}).catch(() => { /* not signed in: the normal case */ });
