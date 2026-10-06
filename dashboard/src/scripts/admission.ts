// /invite/ and /setup/: the one-time secret travels in the URL #fragment (never sent to the server
// in a request line or Referer). Read it, drop it from the address bar/history immediately, bind it
// to this browser via POST /auth/admission-context, then continue to Google.

import { ApiError, api } from '../lib/api';
import { banner, byId, h, replace } from '../lib/dom';

const section = byId('admission');
const status = byId('adm-status');
const kind = section.dataset['kind'] === 'bootstrap' ? 'bootstrap' : 'invitation';
const noun = kind === 'bootstrap' ? 'setup link' : 'invitation link';

const secret = location.hash.replace(/^#/, '').trim();
// Remove the fragment before anything else can observe it (history, screenshots, extensions reading the URL later).
history.replaceState(null, '', location.pathname);

function fail(title: string, message: string): void {
  replace(status, banner('bad', title, message),
    h('p', null, h('a', { attrs: { href: '/login/' }, text: 'Go to sign in' })));
}

function safeAuthorizeUrl(raw: unknown): string | null {
  if (typeof raw !== 'string') return null;
  try {
    const u = new URL(raw, location.origin);
    if (u.origin === location.origin || u.protocol === 'https:') return u.href;
  } catch { /* invalid */ }
  return null;
}

async function main(): Promise<void> {
  if (!secret || secret.length > 512) {
    fail(`This ${noun} is incomplete.`, `Open the full link exactly as you received it. If it was cut off, ask ${kind === 'bootstrap' ? 'the operator' : 'an administrator'} for a new one.`);
    return;
  }
  replace(status, h('p', { class: 'muted', text: `Verifying your ${noun}…` }));
  try {
    const res = await api.post<{ authorize_url: string }>('/auth/admission-context', { kind, secret }, { csrf: false, noAuthRedirect: true });
    const url = safeAuthorizeUrl(res?.authorize_url);
    if (!url) {
      fail('Unexpected response.', 'The server did not return a sign-in address. Please try the link again.');
      return;
    }
    replace(status, h('p', null, 'Link accepted. Continuing to Google sign-in… ', h('a', { attrs: { href: url }, text: 'Continue manually' })));
    location.assign(url);
  } catch (err) {
    if (err instanceof ApiError) {
      if (err.status === 429) {
        fail('Too many attempts.', 'Please wait a few minutes and open the link again.');
      } else if (err.status === 0 || err.status >= 500) {
        fail('Server unavailable.', 'The link could not be checked right now. Your link has not been used; try opening it again shortly.');
      } else if (kind === 'bootstrap') {
        fail('Setup link invalid.', 'This setup link is invalid, expired, or has already been used. The first administrator can only be set up once.');
      } else {
        fail('Invitation invalid or expired.', 'This invitation link is invalid, has expired, or has already been used. Ask an administrator for a new invitation.');
      }
      return;
    }
    fail('Something went wrong.', 'Please try the link again.');
  }
}

void main();
