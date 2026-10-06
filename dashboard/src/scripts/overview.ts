import { allocationBar, barLegend, fmtBytes, hostBars, quotaBars } from '../lib/alloc';
import { ApiError, api } from '../lib/api';
import { banner, byId, h, replace, td, value } from '../lib/dom';
import { DASH, age, pct, rate, usedOf } from '../lib/format';
import { Poller } from '../lib/poll';
import { describeError, errorBlock, initPage, showContent, showLoadError } from '../lib/shell';
import { freshness, opBadge, safetyBadges, stateBadge } from '../lib/status';
import type { Accounting, Container, ContainerList, Me } from '../lib/types';

const SUMMARY_EVERY = 3; // refresh the allocation summary every 3rd tick (30 s)

async function main(): Promise<void> {
  const ctx = await initPage({ what: 'the overview' });
  if (!ctx) return;
  const { me, isAdmin } = ctx;
  if (isAdmin) byId('create-btn').hidden = false;
  byId('summary-title').textContent = isAdmin ? 'Host allocation' : 'Your quota';

  let tick = 0;
  let firstLoad = true;
  const poller: Poller = new Poller({
    interval: 10_000,
    task: async (signal) => {
      const list = await api.get<ContainerList>('/api/containers', { signal });
      renderContainers(list, isAdmin);
      renderCollector(list);
      if (tick % SUMMARY_EVERY === 0) {
        try {
          await renderSummary(me, isAdmin, signal);
        } catch (err) {
          if (err instanceof DOMException && err.name === 'AbortError') throw err;
          replace(byId('summary'), errorBlock(err, 'Could not load allocation'));
        }
      }
      tick++;
      replace(byId('refresh-banner'));
      byId('updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
      if (firstLoad) { firstLoad = false; showContent(); }
    },
    onError: (err, retryMs) => {
      if (firstLoad) { poller.stop(); showLoadError(err, 'the overview'); return; }
      replace(byId('refresh-banner'), banner('warn', 'Could not refresh.',
        `${describeError(err)} Showing the last loaded data; retrying in ${Math.round(retryMs / 1000)} s.`));
    },
  }).start();

  byId('refresh').addEventListener('click', () => { tick = 0; poller.refreshNow(); });
}

async function renderSummary(me: Me, isAdmin: boolean, signal: AbortSignal): Promise<void> {
  const box = byId('summary');
  if (!isAdmin) {
    // Re-read /api/me (not the cached copy) so quota changes show up.
    const fresh = await api.get<Me>('/api/me', { signal });
    const q = fresh.quota ?? me.quota;
    if (!q) { replace(box, h('p', { class: 'muted', text: 'No quota has been assigned to your account yet.' })); return; }
    replace(box,
      h('p', { class: 'section-note', text: 'Quota counts the configured allocations of your containers (running, stopped or frozen), not live usage.' }),
      barLegend(false),
      h('div', { class: 'grid grid-3' }, ...quotaBars(q)));
    return;
  }
  try {
    const acc = await api.get<Accounting>('/api/accounting', { signal });
    const nodes: HTMLElement[] = [];
    if (!acc.host.known) nodes.push(banner('warn', 'Host capacity unknown.', 'LXD has not been observed yet, so host budgets cannot be computed.'));
    if (acc.incomplete.length) {
      nodes.push(banner('warn', 'Accounting incomplete.', `${acc.incomplete.length} item(s) without explicit limits block new allocations. `,
        h('a', { attrs: { href: '/accounting/' }, text: 'See details' })));
    }
    replace(box, ...nodes, barLegend(true),
      h('div', { class: 'grid grid-3' }, ...hostBars(acc.host),
        ...acc.host.pools.map((p) => allocationBar({ title: `Pool ${p.name}`, fmt: fmtBytes, ...p, usedLabel: 'Physically used' }))),
      h('p', { class: 'small' }, 'You are an administrator. ', h('a', { attrs: { href: '/accounting/' }, text: 'Open full accounting' })));
  } catch (err) {
    if (err instanceof ApiError && err.isDenied) {
      replace(box, h('p', { class: 'muted', text: 'Host allocation is not available to your account.' }));
      return;
    }
    throw err;
  }
}

function renderCollector(list: ContainerList): void {
  const c = list.collector;
  const box = byId('collector-banner');
  if (!c) { replace(box); return; }
  if (!c.lxd_available) {
    replace(box, banner('bad', 'LXD is unavailable.', `Last successful collection ${age(c.age_seconds)}. Showing the last known values; they may be out of date.`));
  } else if (c.stale) {
    replace(box, banner('warn', 'Metrics are stale.', `The collector last succeeded ${age(c.age_seconds)}. Showing the last known values.`));
  } else {
    replace(box);
  }
}

function renderContainers(list: ContainerList, isAdmin: boolean): void {
  const box = byId('containers');
  if (!list.containers.length) {
    replace(box, h('div', { class: 'state-box' },
      h('p', { text: isAdmin ? 'No containers yet.' : 'No containers have been assigned to you yet.' }),
      isAdmin
        ? h('a', { class: 'button primary', attrs: { href: '/containers/new/' }, text: 'Create the first container' })
        : h('p', { class: 'small', text: 'Ask an administrator to create one or to give you access.' })));
    return;
  }
  const rows = [...list.containers].sort((a, b) => a.name.localeCompare(b.name)).map(row);
  const th = (text: string, cls = '') => h('th', { class: cls, text, attrs: { scope: 'col' } });
  replace(box, h('div', { class: 'table-wrap' },
    h('table', { class: 'responsive' },
      h('caption', { class: 'sr-only', text: 'Containers with latest metrics' }),
      h('thead', null, h('tr', null,
        th('Name'), th('State'), th('CPU', 'num'), th('Memory', 'num'), th('Disk', 'num'), th('Network RX / TX', 'num'), th('Updated'))),
      h('tbody', null, ...rows))));
}

function row(c: Container): HTMLTableRowElement {
  const m = c.metrics;
  const flags = safetyBadges(c);
  const href = `/containers/view/?id=${encodeURIComponent(c.id)}`;
  return h('tr', null,
    td('Name', [
      h('a', { attrs: { href }, text: c.name }),
      flags.length ? h('span', { class: 'cell-sub badges' }, ...flags) : null,
      c.owner ? h('span', { class: 'cell-sub', text: `owner ${c.owner.email}` }) : null,
    ], 'name-cell'),
    td('State', [h('span', { class: 'badges' }, stateBadge(c), c.active_operation ? opBadge(c.active_operation) : null)]),
    td('CPU', [value(pct(m?.cpu_pct ?? null)), m && m.cpu_pct !== null ? h('span', { class: 'cell-sub', text: 'of allocated cores' }) : null], 'num'),
    td('Memory', usedOf(m?.memory_bytes ?? null, m?.memory_limit_bytes ?? null), 'num'),
    td('Disk', usedOf(m?.disk_bytes ?? null, m?.disk_limit_bytes ?? null), 'num'),
    td('Network', rateCell(m?.rx_rate ?? null, m?.tx_rate ?? null), 'num'),
    td('Updated', freshness(c)));
}

function rateCell(rx: number | null, tx: number | null): HTMLElement | string {
  if (rx === null && tx === null) return DASH;
  return h('span', null,
    h('span', { attrs: { title: 'received' } }, 'RX ', value(rate(rx))), h('br'),
    h('span', { attrs: { title: 'transmitted' } }, 'TX ', value(rate(tx))));
}

void main().catch((err: unknown) => showLoadError(err, 'the overview'));
