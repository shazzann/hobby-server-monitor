// /activity/ (admin): paginated audit events (newest first, `before_id` cursor).

import { api, enc, qs } from '../lib/api';
import { badge, byId, h, replace, setBusy, td } from '../lib/dom';
import { dateTime, since } from '../lib/format';
import { errorBlock, initPage, showContent, showLoadError } from '../lib/shell';
import { outcomeTone } from '../lib/status';
import type { AuditEvent } from '../lib/types';

const PAGE = 50;

interface Page { events: AuditEvent[]; next_before_id: number | string | null }

let cursor: number | string | null = null;
let total = 0;
let tbody: HTMLTableSectionElement;

async function main(): Promise<void> {
  const ctx = await initPage({ adminOnly: true, what: 'the activity log' });
  if (!ctx) return;
  try {
    await loadFirst();
    showContent();
  } catch (err) {
    showLoadError(err, 'the activity log');
    return;
  }
  const more = byId<HTMLButtonElement>('act-more');
  more.addEventListener('click', async () => {
    setBusy(more, true, 'Loading…');
    try { await loadMore(); replace(byId('act-msg')); } catch (err) { replace(byId('act-msg'), errorBlock(err, 'Could not load older events')); }
    setBusy(more, false);
    more.hidden = cursor === null;
  });
  const refresh = byId<HTMLButtonElement>('act-refresh');
  refresh.addEventListener('click', async () => {
    setBusy(refresh, true, 'Loading…');
    try { await loadFirst(); replace(byId('act-msg')); } catch (err) { replace(byId('act-msg'), errorBlock(err, 'Could not refresh')); }
    setBusy(refresh, false);
  });
}

async function loadFirst(): Promise<void> {
  const page = await api.get<Page>(`/api/audit-events${qs({ limit: PAGE })}`);
  total = 0;
  if (!page.events.length) {
    replace(byId('act-table'), h('div', { class: 'state-box', text: 'No activity recorded yet.' }));
    byId('act-more').hidden = true;
    byId('act-count').textContent = '';
    return;
  }
  tbody = h('tbody');
  const th = (t: string) => h('th', { text: t, attrs: { scope: 'col' } });
  replace(byId('act-table'), h('div', { class: 'table-wrap' }, h('table', { class: 'responsive' },
    h('caption', { class: 'sr-only', text: 'Audit events, newest first' }),
    h('thead', null, h('tr', null, th('When'), th('Actor'), th('Action'), th('Target'), th('Outcome'), th('Details'))),
    tbody)));
  append(page);
}

async function loadMore(): Promise<void> {
  if (cursor === null) return;
  append(await api.get<Page>(`/api/audit-events${qs({ before_id: cursor, limit: PAGE })}`));
}

function append(page: Page): void {
  for (const ev of page.events) tbody.appendChild(row(ev));
  total += page.events.length;
  cursor = page.next_before_id;
  byId('act-more').hidden = cursor === null;
  byId('act-count').textContent = `Showing ${total} event${total === 1 ? '' : 's'}${cursor === null ? ' (all)' : ''}.`;
}

function row(ev: AuditEvent): HTMLTableRowElement {
  const target = ev.target_type === 'container' && ev.target_id
    ? h('a', { attrs: { href: `/containers/view/?id=${enc(ev.target_id)}` }, text: ev.target_label ?? ev.target_id })
    : h('span', { text: ev.target_label ?? ev.target_id ?? '—' });
  return h('tr', null,
    td('When', [h('time', { attrs: { datetime: ev.at }, text: dateTime(ev.at) }), h('span', { class: 'cell-sub', text: since(ev.at) })]),
    td('Actor', ev.actor_email ?? 'system'),
    td('Action', h('code', { text: ev.action })),
    td('Target', [target, ev.target_type ? h('span', { class: 'cell-sub', text: ev.target_type }) : null]),
    td('Outcome', badge(ev.outcome, outcomeTone(ev.outcome))),
    td('Details', detailsText(ev.details)));
}

function detailsText(d: unknown): HTMLElement | string {
  if (d === null || d === undefined || (typeof d === 'object' && Object.keys(d as object).length === 0)) return h('span', { class: 'muted', text: 'none' });
  if (typeof d !== 'object') return String(d);
  const text = Object.entries(d as Record<string, unknown>)
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : String(v)}`).join(', ');
  return h('span', { class: 'small mono', text: text.length > 300 ? `${text.slice(0, 300)}…` : text, attrs: { title: text.length > 300 ? text.slice(0, 2000) : undefined } });
}

void main().catch((err: unknown) => showLoadError(err, 'the activity log'));
