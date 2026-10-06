// Text + tone mapping for statuses. Unknown strings fall back to neutral but keep their text.

import { badge, h, type Tone } from './dom';
import type { Container, Operation, OperationState } from './types';

export function containerStateTone(state: string | null | undefined): Tone {
  switch ((state ?? '').toLowerCase()) {
    case 'running': return 'ok';
    case 'stopped': return 'neutral';
    case 'frozen': return 'info';
    case 'error': return 'bad';
    default: return 'warn';
  }
}

export function stateBadge(c: Container): HTMLSpanElement {
  const state = c.metrics?.state ?? null;
  return badge(state ?? 'state unknown', containerStateTone(state));
}

export const TERMINAL_STATES: ReadonlySet<OperationState> = new Set(['succeeded', 'failed', 'cancelled']);

export function isTerminal(state: OperationState): boolean {
  return TERMINAL_STATES.has(state);
}

export function opTone(state: OperationState): Tone {
  switch (state) {
    case 'succeeded': return 'ok';
    case 'failed': return 'bad';
    case 'cancelled': return 'neutral';
    case 'reconciling': return 'warn';
    default: return 'info';
  }
}

const OP_TEXT: Record<OperationState, string> = {
  queued: 'queued',
  running: 'running',
  succeeded: 'succeeded',
  failed: 'failed',
  reconciling: 'reconciling (outcome being verified)',
  cancelled: 'cancelled',
};

export function opBadge(op: Pick<Operation, 'state' | 'kind'>, withKind = true): HTMLSpanElement {
  return badge(`${withKind ? `${op.kind}: ` : ''}${OP_TEXT[op.state] ?? op.state}`, opTone(op.state));
}

/** "unmanaged", "unsafe: <reason>" labels; empty when managed and safe. */
export function safetyBadges(c: Container): HTMLElement[] {
  const out: HTMLElement[] = [];
  if (!c.managed) out.push(badge('unmanaged', 'warn', 'Not created by this dashboard; controls are disabled'));
  if (c.safety && c.safety !== 'safe') {
    const reasons = c.safety_reasons.length ? c.safety_reasons.join(', ') : c.safety;
    out.push(badge(`unsafe: ${reasons}`, 'bad', 'Failed the container safety policy; controls are disabled'));
  }
  if (c.status && c.status !== 'active') out.push(badge(c.status, 'neutral'));
  return out;
}

export function controllable(c: Container): boolean {
  return c.managed && (c.safety === 'safe' || c.safety === null) && (c.status === 'active' || c.status === null);
}

export function freshness(c: Container): HTMLElement {
  const m = c.metrics;
  if (!m) return h('span', null, badge('no data yet', 'neutral'));
  const ageText = m.age_seconds === null ? 'age unknown' : ageShort(m.age_seconds);
  return h('span', { class: 'badges' },
    h('span', { class: 'num', text: ageText }),
    m.stale ? badge('stale', 'warn', 'Older than 3 collection intervals') : null);
}

function ageShort(sec: number): string {
  if (sec < 60) return `${Math.round(sec)} s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)} h ago`;
  return `${Math.round(sec / 86400)} d ago`;
}

export function outcomeTone(outcome: string): Tone {
  const o = outcome.toLowerCase();
  if (/(success|succeeded|^ok$|allowed|completed)/.test(o)) return 'ok';
  if (/(fail|error|denied|rejected|forbidden)/.test(o)) return 'bad';
  if (/(pending|queued|running|started|intent|reconcil)/.test(o)) return 'warn';
  return 'neutral';
}
