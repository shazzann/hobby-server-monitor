// Allocation bars: allocated + pending versus a budget, with the host reserve shown separately.
// Widths are set through CSSOM (element.style.width), which the CSP allows.

import { h, value } from './dom';
import { DASH, bytes, cores, fraction } from './format';
import type { HostResource, QuotaSummary, ResourceTriple } from './types';

type Fmt = (v: number | null) => string;

const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

function seg(cls: string, part: number | null, scale: number, label: string): HTMLElement | null {
  if (!isNum(part) || part <= 0 || scale <= 0) return null;
  const s = h('span', { class: cls, attrs: { title: label } });
  s.style.width = `${Math.min(100, (part / scale) * 100).toFixed(2)}%`;
  return s;
}

export interface BarInput {
  title: string;
  fmt: Fmt;
  allocated: number | null;
  pending: number | null;
  /** The amount that may be allocated (quota or host budget). */
  budget: number | null;
  remaining: number | null;
  total?: number | null;
  reserve?: number | null;
  used?: number | null;
  usedLabel?: string;
}

export function allocationBar(b: BarInput): HTMLElement {
  const scale = isNum(b.total) && b.total > 0 ? b.total : isNum(b.budget) ? b.budget : 0;
  const committed = (b.allocated ?? 0) + (b.pending ?? 0);
  const over = isNum(b.budget) && committed > b.budget;
  const share = isNum(b.budget) && b.budget > 0 ? committed / b.budget : null;

  const bar = h('div', { class: 'bar', attrs: { 'aria-hidden': 'true' } },
    seg(over ? 'seg-over' : 'seg-allocated', b.allocated, scale, `allocated ${b.fmt(b.allocated)}`),
    seg('seg-pending', b.pending, scale, `pending ${b.fmt(b.pending)}`),
    seg('seg-reserve', isNum(b.total) ? b.reserve ?? null : null, scale, `reserve ${b.fmt(b.reserve ?? null)}`));

  const summary = `${b.fmt(b.allocated)} allocated + ${b.fmt(b.pending)} pending of ${b.fmt(b.budget)} budget`;
  const details: (string | HTMLElement)[] = [];
  if (isNum(b.total)) details.push(`Host total ${b.fmt(b.total)}; ${b.fmt(b.reserve ?? null)} kept in reserve for the host and never allocated.`);
  if (b.used !== undefined) details.push(`${b.usedLabel ?? 'Physically used'}: ${b.fmt(b.used ?? null)}.`);

  return h('div', { class: 'alloc' },
    h('div', { class: 'alloc-head' },
      h('span', { class: 'title', text: b.title }),
      h('span', { class: 'num small' }, 'Remaining ', value(b.fmt(b.remaining)),
        over ? h('span', { class: 'badge bad', text: 'over budget' }) : share !== null ? ` · ${fraction(share)} committed` : null)),
    bar,
    h('div', { class: 'small num', text: scale ? summary : `${summary} (capacity unknown)` }),
    details.length ? h('div', { class: 'small muted' }, ...details.map((d) => h('div', null, d))) : null);
}

export function barLegend(withReserve: boolean): HTMLElement {
  return h('ul', { class: 'legend', attrs: { 'aria-label': 'Bar legend' } },
    h('li', null, h('span', { class: 'sw allocated', attrs: { 'aria-hidden': 'true' } }), 'allocated'),
    h('li', null, h('span', { class: 'sw pending', attrs: { 'aria-hidden': 'true' } }), 'pending (reserved by in-flight operations)'),
    withReserve ? h('li', null, h('span', { class: 'sw reserve', attrs: { 'aria-hidden': 'true' } }), 'host reserve') : null);
}

export const fmtCores: Fmt = (v) => cores(v);
export const fmtBytes: Fmt = (v) => bytes(v);

export function hostBars(r: { cpu: HostResource; memory: HostResource }): HTMLElement[] {
  return [
    allocationBar({ title: 'CPU', fmt: fmtCores, ...r.cpu }),
    allocationBar({ title: 'Memory', fmt: fmtBytes, ...r.memory }),
  ];
}

const RES: [keyof ResourceTriple, string, Fmt][] = [
  ['cpu_cores', 'CPU', fmtCores],
  ['memory_bytes', 'Memory', fmtBytes],
  ['disk_bytes', 'Disk', fmtBytes],
];

/** Quota bars for one user (own quota on the overview, users page). */
export function quotaBars(q: QuotaSummary): HTMLElement[] {
  return RES.map(([k, title, fmt]) => allocationBar({
    title, fmt,
    allocated: q.allocated?.[k] ?? null,
    pending: q.pending?.[k] ?? null,
    budget: q.quota?.[k] ?? null,
    remaining: q.remaining?.[k] ?? null,
  }));
}

/** Compact "allocated (+pending) / quota" text. */
export function quotaText(k: keyof ResourceTriple, quota: ResourceTriple | null, allocated: ResourceTriple | null, pending: ResourceTriple | null): string {
  const fmt = RES.find((r) => r[0] === k)![2];
  const q = quota?.[k] ?? null, a = allocated?.[k] ?? null, p = pending?.[k] ?? null;
  if (q === null && a === null) return DASH;
  return `${fmt(a)}${isNum(p) && p > 0 ? ` (+${fmt(p)} pending)` : ''} of ${fmt(q)}`;
}
