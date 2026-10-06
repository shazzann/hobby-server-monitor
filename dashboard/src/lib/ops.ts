// Durable operations: poll a pending operation (~750 ms) until it reaches a terminal state.

import { ApiError, api, enc } from './api';
import { h, kv } from './dom';
import { dateTime } from './format';
import { Poller } from './poll';
import { isTerminal, opBadge } from './status';
import type { Operation } from './types';

export function watchOperation(id: string, onUpdate: (op: Operation) => void, onFatal?: (err: ApiError) => void): Poller {
  const startedAt = Date.now();
  const poller = new Poller({
    // Fast while an operation is fresh; slow down for long reconciliations so we never hammer the API.
    interval: () => (Date.now() - startedAt < 60_000 ? 750 : 3000),
    maxBackoffMs: 15_000,
    task: async (signal) => {
      try {
        const op = await api.get<Operation>(`/api/operations/${enc(id)}`, { signal });
        onUpdate(op);
        return !isTerminal(op.state);
      } catch (err) {
        if (err instanceof ApiError && (err.isDenied || err.isNotFound)) {
          onFatal?.(err);
          return false;
        }
        throw err;
      }
    },
  });
  return poller.start();
}

/** Mutation responses may be an Operation or `{operation: Operation, ...}`. */
export function asOperation(res: unknown): Operation | null {
  if (!res || typeof res !== 'object') return null;
  const r = res as Record<string, unknown>;
  const inner = r['operation'];
  if (inner && typeof inner === 'object' && 'id' in inner && 'state' in inner) return inner as Operation;
  if (typeof r['id'] === 'string' && typeof r['state'] === 'string' && typeof r['kind'] === 'string') return r as unknown as Operation;
  return null;
}

export function operationSummary(op: Operation): HTMLElement {
  const rows: [string, string | HTMLElement | (string | HTMLElement)[]][] = [
    ['Operation', h('code', { text: op.id })],
    ['Status', opBadge(op)],
    ['Requested', dateTime(op.created_at)],
  ];
  if (op.started_at) rows.push(['Started', dateTime(op.started_at)]);
  if (op.finished_at) rows.push(['Finished', dateTime(op.finished_at)]);
  if (op.error) rows.push(['Error', [h('code', { text: op.error.code }), ` ${op.error.message}`]]);
  if (op.state === 'reconciling') rows.push(['Note', 'The outcome is uncertain and is being verified against LXD. It will not be retried automatically.']);
  return kv(rows);
}
