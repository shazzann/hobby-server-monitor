// /containers/view/?id=<uuid>[&op=<operation id>]

import { api, enc } from '../../lib/api';
import { badge, banner, byId, h, kv, replace, value } from '../../lib/dom';
import { DASH, age, bytes, cores, dateTime, duration, num, pct, rate, usedOf, yesNo } from '../../lib/format';
import { operationSummary, watchOperation } from '../../lib/ops';
import { Poller } from '../../lib/poll';
import { describeError, initPage, queryParam, showContent, showLoadError, showState } from '../../lib/shell';
import { isTerminal, opBadge, safetyBadges, stateBadge } from '../../lib/status';
import type { ContainerDetail, Operation } from '../../lib/types';
import type { ViewCtx } from './context';
import { setupAdmin } from './admin';
import { setupExec } from './exec';
import { setupHistory } from './history';

const UUIDish = /^[A-Za-z0-9-]{1,64}$/;

async function main(): Promise<void> {
  const id = queryParam('id');
  const ctxPage = await initPage({ what: 'this container' });
  if (!ctxPage) return;
  if (!id || !UUIDish.test(id)) {
    showState('error', 'No container selected', 'The link is missing a valid container id.', h('p', null, h('a', { attrs: { href: '/' }, text: 'Back to overview' })));
    return;
  }

  let detail: ContainerDetail | null = null;
  const listeners: ((c: ContainerDetail) => void)[] = [];
  let trackedOpId: string | null = null;
  let opPoller: Poller | null = null;
  let deleted = false;

  const ctx: ViewCtx = {
    id,
    me: ctxPage.me,
    isAdmin: ctxPage.isAdmin,
    current: () => detail!,
    refresh: () => poller.refreshNow(),
    track,
    onUpdate: (fn) => listeners.push(fn),
  };

  function track(op: Operation): void {
    if (op.kind === 'exec') return; // exec results are shown in the command panel
    if (trackedOpId === op.id && opPoller?.running) return;
    opPoller?.stop();
    trackedOpId = op.id;
    renderOp(op);
    if (isTerminal(op.state)) { onTerminal(op); return; }
    opPoller = watchOperation(op.id, (o) => {
      renderOp(o);
      if (isTerminal(o.state)) onTerminal(o);
    }, (err) => {
      replace(byId('op-panel'), banner('warn', 'Operation status unavailable.', describeError(err)));
    });
  }

  function onTerminal(op: Operation): void {
    if (op.kind === 'delete' && op.state === 'succeeded') {
      deleted = true;
      poller.stop();
      showState('empty', 'Container deleted', 'The container was deleted and its allocation released.',
        h('p', null, h('a', { attrs: { href: '/' }, text: 'Back to overview' })));
      return;
    }
    poller.refreshNow();
  }

  function renderOp(op: Operation): void {
    const tone = op.state === 'succeeded' ? 'ok' : op.state === 'failed' ? 'bad' : op.state === 'reconciling' ? 'warn' : 'info';
    const title = isTerminal(op.state) ? `Operation ${op.kind} ${op.state}.` : `Operation ${op.kind} in progress.`;
    replace(byId('op-panel'), h('div', { class: `banner ${tone}` },
      h('p', null, h('span', { class: 'banner-title', text: title })),
      operationSummary(op),
      isTerminal(op.state) ? h('p', { class: 'small' }, h('button', { class: 'link', text: 'Dismiss', attrs: { type: 'button' }, on: { click: () => replace(byId('op-panel')) } })) : null));
  }

  let first = true;
  const poller: Poller = new Poller({
    interval: 10_000,
    task: async (signal) => {
      const c = await api.get<ContainerDetail>(`/api/containers/${enc(id)}`, { signal });
      if (deleted) return false;
      detail = c;
      renderHeader(c);
      renderSummary(c);
      replace(byId('c-banners'), ...banners(c));
      byId('c-updated').textContent = `updated ${new Date().toLocaleTimeString()}`;
      if (first) {
        first = false;
        showContent();
        setupHistory(ctx);
        setupExec(ctx);
        if (ctx.isAdmin) setupAdmin(ctx);
        const opId = queryParam('op');
        if (opId && UUIDish.test(opId)) {
          track({ id: opId, kind: c.active_operation?.id === opId ? c.active_operation.kind : 'operation', state: 'queued', container_id: id, created_at: null, started_at: null, finished_at: null, error: null, result: null });
          history.replaceState(null, '', `${location.pathname}?id=${enc(id)}`);
        }
      }
      if (c.active_operation && c.active_operation.kind !== 'exec' && !isTerminal(c.active_operation.state) && trackedOpId !== c.active_operation.id) {
        track({ ...c.active_operation, container_id: id, created_at: null, started_at: null, finished_at: null, error: null, result: null });
      }
      for (const fn of listeners) fn(c);
      return true;
    },
    onError: (err, retryMs) => {
      if (first) { poller.stop(); showLoadError(err, 'this container'); return; }
      replace(byId('c-banners'), banner('warn', 'Could not refresh.', `${describeError(err)} Showing the last loaded data; retrying in ${Math.round(retryMs / 1000)} s.`),
        ...(detail ? banners(detail) : []));
    },
  }).start();
}

function renderHeader(c: ContainerDetail): void {
  byId('c-name').textContent = c.name;
  document.title = `${c.name} · Hobby Server Monitor`;
  replace(byId('c-badges'), stateBadge(c), ...safetyBadges(c), c.ephemeral ? badge('ephemeral', 'info', 'Deleted by LXD when stopped') : null,
    c.active_operation && !isTerminal(c.active_operation.state) ? opBadge(c.active_operation) : null);
}

function banners(c: ContainerDetail): HTMLElement[] {
  const out: HTMLElement[] = [];
  if (!c.metrics) out.push(banner('info', 'No metrics yet.', 'This container has not been observed by the collector yet.'));
  else if (c.metrics.stale) out.push(banner('warn', 'Metrics are stale.', `Last sample ${age(c.metrics.age_seconds)}. Values below are the last known ones.`));
  if (!c.managed) out.push(banner('warn', 'Unmanaged container.', 'It was not created by this dashboard. Lifecycle, limit and command controls are disabled.'));
  if (c.safety && c.safety !== 'safe') out.push(banner('bad', 'Unsafe container.', `It fails the safety policy (${c.safety_reasons.join(', ') || c.safety}). Controls are disabled.`));
  return out;
}

function renderSummary(c: ContainerDetail): void {
  const m = c.metrics;
  const live = h('div', { class: 'stat-grid' },
    statBox('State', m?.state ?? DASH),
    statBox('CPU', pct(m?.cpu_pct ?? null), `of ${cores(c.limits?.cpu_cores ?? null)} at ${pct(c.limits?.cpu_allowance_pct ?? null, 0)} allowance`),
    statBox('Memory', usedOf(m?.memory_bytes ?? null, m?.memory_limit_bytes ?? null)),
    statBox('Disk', usedOf(m?.disk_bytes ?? null, m?.disk_limit_bytes ?? null)),
    statBox('Network RX', rate(m?.rx_rate ?? null), `${bytes(m?.rx_bytes ?? null)} total`),
    statBox('Network TX', rate(m?.tx_rate ?? null), `${bytes(m?.tx_bytes ?? null)} total`),
  );
  const uptime = m?.uptime_seconds ?? null;
  const details = h('div', { class: 'grid grid-2' },
    kv([
      ['OS / image', [value(c.image_description ?? c.os ?? DASH), c.architecture ? h('span', { class: 'muted', text: ` (${c.architecture})` }) : null]],
      ['IPv4', m?.ipv4 ? h('code', { text: m.ipv4 }) : DASH],
      ['Processes', num(m?.processes ?? null)],
      ['Uptime', uptime === null ? DASH : duration(uptime)],
      ['Started', dateTime(m?.started_at ?? null)],
      ['Last sample', m ? `${dateTime(m.sampled_at)} (${age(m.age_seconds)})` : DASH],
      ['Data quality', m && m.quality.length ? m.quality.join(', ') : 'complete'],
    ]),
    kv([
      ['Owner', c.owner?.email ?? DASH],
      ['Limits', c.limits ? `${cores(c.limits.cpu_cores)} at ${pct(c.limits.cpu_allowance_pct ?? null, 0)}, ${bytes(c.limits.memory_bytes)} RAM, ${bytes(c.limits.disk_bytes)} disk` : DASH],
      ['Storage pool', c.limits?.pool ?? c.observed_limits?.pool ?? DASH],
      ['Observed in LXD', c.observed_limits ? `${cores(c.observed_limits.cpu_cores)}, ${bytes(c.observed_limits.memory_bytes)} RAM, ${bytes(c.observed_limits.disk_bytes)} disk` : DASH],
      ['Ephemeral', yesNo(c.ephemeral)],
      ['Autostart', yesNo(c.autostart)],
      ['Safety', c.safety ? (c.safety === 'safe' ? 'safe' : `${c.safety}: ${c.safety_reasons.join(', ')}`) : DASH],
      ['Description', c.description ? c.description : h('span', { class: 'muted', text: 'none' })],
    ]));
  replace(byId('c-summary'), live, h('hr', { class: 'sep' }), details);
}

function statBox(label: string, val: string, sub?: string): HTMLElement {
  return h('div', { class: 'stat' },
    h('div', { class: 'label', text: label }),
    h('div', { class: 'value' }, value(val)),
    sub ? h('div', { class: 'sub', text: sub }) : null);
}

void main().catch((err: unknown) => showLoadError(err, 'this container'));
