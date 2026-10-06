// Page bootstrap shared by all authenticated pages: loads /api/me, fills the header,
// reveals admin-only nav links (cosmetic; the API enforces), wires logout, and renders
// page-level loading / denied / error states.

import { ApiError, getMe, logout } from './api';
import { badge, byId, h, replace, type Child } from './dom';
import type { Me } from './types';

export interface PageContext {
  me: Me;
  isAdmin: boolean;
  /** Element where page content is rendered. */
  root: HTMLElement;
}

const stateEl = () => byId('page-state');
const contentEl = () => byId('page-content');

export function showLoading(text = 'Loading…'): void {
  const s = stateEl();
  s.hidden = false;
  replace(s, h('div', { class: 'state-box', attrs: { 'aria-busy': 'true' } }, text));
  contentEl().hidden = true;
}

export function showContent(): void {
  stateEl().hidden = true;
  replace(stateEl());
  contentEl().hidden = false;
}

export function showState(kind: 'denied' | 'error' | 'empty', title: string, ...body: Child[]): void {
  const s = stateEl();
  s.hidden = false;
  replace(s, h('div', { class: `state-box ${kind}`, attrs: { role: kind === 'error' ? 'alert' : 'status' } },
    h('h2', { text: title }), ...body.map((b) => (typeof b === 'string' ? h('p', { text: b }) : b))));
  contentEl().hidden = true;
}

/** Renders the right page-level state for an error from a primary load. */
export function showLoadError(err: unknown, what = 'this page'): void {
  if (err instanceof ApiError) {
    if (err.status === 401) { showLoading('Your session has ended. Redirecting to sign in…'); return; }
    if (err.isDenied) {
      showState('denied', 'Access denied', `You do not have permission to view ${what}.`, h('p', null, h('a', { attrs: { href: '/' }, text: 'Back to overview' })));
      return;
    }
    if (err.isNotFound) {
      showState('error', 'Not found', `${capitalize(what)} does not exist or was deleted.`, h('p', null, h('a', { attrs: { href: '/' }, text: 'Back to overview' })));
      return;
    }
    showState('error', 'Could not load', describeError(err), retryLink());
    return;
  }
  showState('error', 'Could not load', 'An unexpected error occurred.', retryLink());
}

function retryLink(): HTMLElement {
  return h('p', null, h('button', { text: 'Try again', attrs: { type: 'button' }, on: { click: () => location.reload() } }));
}

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Human-readable error with code and request id (for support). */
export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    const ref = err.requestId ? ` (ref ${err.requestId})` : '';
    const code = err.code && !err.code.startsWith('HTTP_') && err.code !== 'NETWORK' ? ` [${err.code}]` : '';
    return `${err.message}${code}${ref}`;
  }
  if (err instanceof Error) return err.message;
  return 'Unexpected error.';
}

/** An inline error block for a form or section, including field errors. */
export function errorBlock(err: unknown, title = 'Request failed'): HTMLElement {
  const fields = err instanceof ApiError ? Object.entries(err.fields) : [];
  return h('div', { class: 'banner bad', attrs: { role: 'alert' } },
    h('p', null, h('span', { class: 'banner-title', text: `${title}: ` }), describeError(err)),
    fields.length ? h('ul', null, ...fields.map(([k, v]) => h('li', null, h('code', { text: k }), `: ${v}`))) : null);
}

/**
 * Loads the session for an authenticated page. Returns null when the page must not render
 * (redirecting to login, or admin-only page for a non-admin: a denied state is shown).
 */
export async function initPage(opts: { adminOnly?: boolean; what?: string } = {}): Promise<PageContext | null> {
  showLoading();
  wireLogout();
  let me: Me;
  try {
    me = await getMe();
  } catch (err) {
    if (err instanceof ApiError && err.status !== 401 && !err.isDenied) {
      showState('error', 'Could not load your session', describeError(err), retryLink());
    } else {
      showLoadError(err, opts.what);
    }
    return null;
  }
  const isAdmin = me.user.role === 'admin';
  fillHeader(me, isAdmin);
  if (opts.adminOnly && !isAdmin) {
    showState('denied', 'Administrators only', `Only administrators can view ${opts.what ?? 'this page'}.`,
      h('p', null, h('a', { attrs: { href: '/' }, text: 'Back to overview' })));
    return null;
  }
  return { me, isAdmin, root: contentEl() };
}

function fillHeader(me: Me, isAdmin: boolean): void {
  const who = document.getElementById('who');
  if (who) {
    replace(who,
      h('span', { class: 'email', text: me.user.email, attrs: { title: me.user.email } }),
      badge(me.user.role, isAdmin ? 'info' : 'neutral'),
      byId('logout-btn'));
    who.hidden = false;
  }
  if (isAdmin) document.querySelectorAll<HTMLElement>('[data-admin-only]').forEach((n) => { n.hidden = false; });
}

function wireLogout(): void {
  const btn = document.getElementById('logout-btn') as HTMLButtonElement | null;
  if (!btn || btn.dataset['wired']) return;
  btn.dataset['wired'] = '1';
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = 'Signing out…';
    void logout();
  });
}

export function queryParam(name: string): string | null {
  return new URLSearchParams(location.search).get(name);
}
