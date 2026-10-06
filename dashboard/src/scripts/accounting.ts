// /accounting/ (admin): host and per-owner allocation, per-container measured consumption.

import { allocationBar, barLegend, fmtBytes, hostBars, quotaText } from '../lib/alloc';
import { api, enc, qs } from '../lib/api';
import { banner, byId, h, replace, setBusy, stat, td } from '../lib/dom';
import { DASH, bytes, cores, dateTime, duration, fraction, num } from '../lib/format';
import { errorBlock, initPage, showContent, showLoadError } from '../lib/shell';
import type { Accounting, ContainerList, Usage } from '../lib/types';

async function main(): Promise<void> {
  const ctx = await initPage({ adminOnly: true, what: 'accounting' });
  if (!ctx) return;
  try {
    await Promise.all([loadAccounting(), loadContainers()]);
    showContent();
  } catch (err) {
    showLoadError(err, 'accounting');
    return;
  }
  const refresh = byId<HTMLButtonElement>('acc-refresh');
  refresh.addEventListener('click', async () => {
    setBusy(refresh, true, 'Refreshing…');
    try { await loadAccounting(); replace(byId('acc-msg')); } catch (err) { replace(byId('acc-msg'), errorBlock(err, 'Could not refresh')); }
    setBusy(refresh, false);
  });
  setupUsage();
}

async function loadAccounting(): Promise<void> {
  const acc = await api.get<Accounting>('/api/accounting');
  const host = byId('host');
  if (!acc.host.known) {
    replace(host, banner('warn', 'Host capacity unknown.', 'LXD has not been observed yet, so totals and budgets are unavailable. Allocations are still listed per owner below.'));
  } else {
    replace(host, barLegend(true), h('div', { class: 'grid grid-3' },
      ...hostBars(acc.host),
      ...acc.host.pools.map((p) => allocationBar({ title: `Pool ${p.name}${p.driver ? ` (${p.driver})` : ''}`, fmt: fmtBytes, ...p, usedLabel: 'Physically used (images, volumes, snapshots)' }))),
    acc.host.pools.length ? null : h('p', { class: 'muted', text: 'No storage pools discovered.' }));
  }

  const inc = byId('inc-section');
  inc.hidden = acc.incomplete.length === 0;
  replace(byId('incomplete'), ...acc.incomplete.map((item) => h('li', { text: describeIncomplete(item) })));

  const owners = [...acc.owners].sort((a, b) => a.email.localeCompare(b.email));
  replace(byId('owners'), owners.length ? h('div', { class: 'table-wrap' }, h('table', { class: 'responsive' },
    h('caption', { class: 'sr-only', text: 'Allocation per owner' }),
    h('thead', null, h('tr', null, ...['Owner', 'CPU', 'Memory', 'Disk', 'Remaining'].map((t) => h('th', { text: t, attrs: { scope: 'col' } })))),
    h('tbody', null, ...owners.map((o) => h('tr', null,
      td('Owner', o.email, 'name-cell'),
      td('CPU', quotaText('cpu_cores', o.quota, o.allocated, o.pending)),
      td('Memory', quotaText('memory_bytes', o.quota, o.allocated, o.pending)),
      td('Disk', quotaText('disk_bytes', o.quota, o.allocated, o.pending)),
      td('Remaining', `${cores(o.remaining?.cpu_cores ?? null)}, ${bytes(o.remaining?.memory_bytes ?? null)}, ${bytes(o.remaining?.disk_bytes ?? null)} disk`))))))
    : h('div', { class: 'state-box', text: 'No owners with quotas yet.' }));
}

/** `incomplete` items have no fixed shape in the contract: show the most descriptive fields as text. */
function describeIncomplete(item: unknown): string {
  if (typeof item === 'string') return item;
  if (item && typeof item === 'object') {
    const r = item as Record<string, unknown>;
    const name = r['name'] ?? r['container_name'] ?? r['container'] ?? r['target_label'] ?? r['id'];
    const reason = r['reason'] ?? r['message'] ?? r['missing'];
    if (name !== undefined || reason !== undefined) {
      return [name !== undefined ? String(name) : null, reason !== undefined ? (Array.isArray(reason) ? reason.join(', ') : String(reason)) : null].filter(Boolean).join(': ');
    }
    return JSON.stringify(item);
  }
  return String(item);
}

async function loadContainers(): Promise<void> {
  const list = await api.get<ContainerList>('/api/containers');
  const sel = byId<HTMLSelectElement>('use-container');
  const sorted = [...list.containers].sort((a, b) => a.name.localeCompare(b.name));
  replace(sel, ...(sorted.length ? sorted.map((c) => h('option', { attrs: { value: c.id }, text: c.managed ? c.name : `${c.name} (unmanaged)` })) : [h('option', { attrs: { value: '' }, text: 'No containers' })]));
  sel.disabled = !sorted.length;
  byId<HTMLButtonElement>('use-submit').disabled = !sorted.length;
}

function setupUsage(): void {
  const form = byId<HTMLFormElement>('use-form');
  const sel = byId<HTMLSelectElement>('use-container');
  const range = byId<HTMLSelectElement>('use-range');
  const btn = byId<HTMLButtonElement>('use-submit');
  const out = byId('use-result');
  let seq = 0;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!sel.value) return;
    const mySeq = ++seq;
    const name = sel.selectedOptions[0]?.textContent ?? '';
    setBusy(btn, true, 'Loading…');
    try {
      const u = await api.get<Usage>(`/api/containers/${enc(sel.value)}/usage${qs({ range: range.value })}`);
      if (mySeq !== seq) return;
      replace(out, renderUsage(u, name));
    } catch (err) {
      if (mySeq === seq) replace(out, errorBlock(err, 'Could not load usage'));
    } finally {
      setBusy(btn, false);
    }
  });
}

function renderUsage(u: Usage, name: string): HTMLElement {
  const period = `${dateTime(new Date(u.start * 1000).toISOString())} to ${dateTime(new Date(u.end * 1000).toISOString())}`;
  if (!u.coverage || !u.covered_seconds) {
    return h('div', { class: 'state-box' }, h('p', { text: `No measured usage for ${name} in this period.` }), h('p', { class: 'small', text: period }));
  }
  return h('div', null,
    h('p', { class: 'small muted', text: `${name}: ${period}. Measured ${duration(u.covered_seconds)} (${fraction(u.coverage)} coverage).` }),
    h('div', { class: 'stat-grid' },
      stat('CPU', u.cpu_core_hours === null ? DASH : `${num(u.cpu_core_hours, 3)} core-hours`),
      stat('Memory', u.memory_gib_hours === null ? DASH : `${num(u.memory_gib_hours, 3)} GiB-hours`),
      stat('Disk average', bytes(u.disk_avg_bytes), `max ${bytes(u.disk_max_bytes)}`),
      stat('Received', bytes(u.rx_bytes)),
      stat('Sent', bytes(u.tx_bytes)),
      stat('Coverage', fraction(u.coverage))));
}

void main().catch((err: unknown) => showLoadError(err, 'accounting'));
