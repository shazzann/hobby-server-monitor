// Command panel: one command at a time, each in a fresh `/bin/sh -c` inside the container (wrapped in `timeout -s KILL`).
// Output is rendered with textContent in <pre>; never as HTML.

import { ApiError, Submission, api, enc } from '../../lib/api';
import { badge, banner, byId, h, kv, replace, setBusy } from '../../lib/dom';
import { ms } from '../../lib/format';
import { asOperation, watchOperation } from '../../lib/ops';
import { isTerminal, opBadge } from '../../lib/status';
import { errorBlock } from '../../lib/shell';
import type { ContainerDetail, Operation } from '../../lib/types';
import type { ViewCtx } from './context';

const MAX_BYTES = 4096;
const KEEP_RESULTS = 5;
const encoder = new TextEncoder();

interface Run { command: string; op: Operation; node: HTMLElement }

export function setupExec(ctx: ViewCtx): void {
  const section = byId('exec-section');
  const body = byId('exec-body');
  const initial = ctx.current();
  section.hidden = false;

  if (!initial.capabilities.can_exec) {
    replace(body, h('p', { class: 'muted', text: execUnavailableReason(initial) }));
    ctx.onUpdate((c) => { if (!c.capabilities.can_exec) replace(body, h('p', { class: 'muted', text: execUnavailableReason(c) })); });
    return;
  }

  const runAs = initial.capabilities.exec_as ?? 'unknown user';
  const cmd = h('textarea', { attrs: { id: 'exec-cmd', rows: 2, spellcheck: 'false', autocomplete: 'off', autocapitalize: 'off', 'aria-describedby': 'exec-hint exec-count', required: true } });
  const count = h('span', { attrs: { id: 'exec-count', 'aria-live': 'polite' }, class: 'hint num' });
  const run = h('button', { class: 'primary', text: 'Run', attrs: { type: 'submit' } });
  const status = h('div', { attrs: { 'aria-live': 'polite' } });
  const results = h('div');
  const form = h('form', { attrs: { novalidate: true } },
    h('div', { class: 'field' },
      h('label', { attrs: { for: 'exec-cmd' }, text: 'Command' }),
      cmd,
      h('span', { class: 'hint', attrs: { id: 'exec-hint' } },
        `Runs as ${runAs} with /bin/sh -c. Each command starts in a fresh shell: working directory and variables do not persist (no lasting cd), and interactive programs are not supported. Output is size-limited and long commands time out. Press Ctrl+Enter to run.`),
      count),
    h('div', { class: 'row' }, run));
  replace(body, form, status, results);

  const submission = new Submission();
  const runs: Run[] = [];
  let pending = false;

  const validate = (): string | null => {
    const text = cmd.value;
    const n = encoder.encode(text).length;
    count.textContent = `${n} / ${MAX_BYTES} bytes`;
    if (!text.trim()) return 'Enter a command.';
    if (n > MAX_BYTES) return `The command is ${n} bytes; the limit is ${MAX_BYTES} bytes.`;
    if (text.includes('\u0000')) return 'The command must not contain NUL characters.';
    return null;
  };
  const sync = () => {
    const err = validate();
    cmd.setAttribute('aria-invalid', String(Boolean(err) && cmd.value.length > 0));
    run.disabled = pending || Boolean(err);
  };
  cmd.addEventListener('input', sync);
  cmd.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); }
  });
  sync();

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const err = validate();
    if (err) { replace(status, banner('warn', err)); return; }
    if (pending) return;
    void submit(cmd.value);
  });

  async function submit(command: string): Promise<void> {
    pending = true;
    setBusy(run, true, 'Running…');
    replace(status, h('p', { class: 'muted', text: 'Submitting…' }));
    const reqBody = { command };
    try {
      const op = asOperation(await api.post<unknown>(`/api/containers/${enc(ctx.id)}/exec`, reqBody, { idempotencyKey: submission.keyFor(reqBody) }));
      if (!op) throw new Error('The server accepted the command but did not return an operation id.');
      submission.settle();
      replace(status);
      const entry: Run = { command, op, node: h('div', { class: 'run-result' }) };
      runs.unshift(entry);
      while (runs.length > KEEP_RESULTS) runs.pop()?.node.remove();
      results.prepend(entry.node);
      renderRun(entry);
      if (isTerminal(op.state)) { done(); return; }
      watchOperation(op.id, (o) => {
        entry.op = o;
        renderRun(entry);
        if (isTerminal(o.state)) done();
      }, (fatal) => {
        replace(status, errorBlock(fatal, 'Lost access to the command result'));
        done();
      });
    } catch (err) {
      submission.settle(err);
      const retry = err instanceof ApiError && err.isRetryable
        ? ' The command may or may not have been accepted. Pressing Run again with the same text reuses the same request id, so it will not run twice.'
        : '';
      replace(status, errorBlock(err, 'Command not started'), retry ? h('p', { class: 'small', text: retry.trim() }) : null);
      done();
    }
  }

  function done(): void {
    pending = false;
    setBusy(run, false);
    sync();
  }

  ctx.onUpdate((c: ContainerDetail) => {
    if (!c.capabilities.can_exec && !pending) {
      run.disabled = true;
      replace(status, banner('warn', 'Commands unavailable.', execUnavailableReason(c)));
    }
  });
}

function renderRun(r: Run): void {
  const { op } = r;
  const res = op.result;
  const children: (HTMLElement | null)[] = [
    h('h3', null, h('span', { 'class': 'muted', text: '$ ' }), r.command.length > 300 ? `${r.command.slice(0, 300)}…` : r.command),
    h('div', { class: 'badges' }, opBadge(op, false), res ? outcomeBadge(res.outcome, res.exit_code) : null),
  ];
  const rows: [string, string | HTMLElement][] = [['Operation', h('code', { text: op.id })]];
  if (res) {
    rows.push(['Exit code', res.exit_code === null ? 'unknown' : String(res.exit_code)]);
    rows.push(['Duration', ms(res.duration_ms)]);
    rows.push(['Ran as', res.user]);
  }
  children.push(kv(rows));

  if (op.state === 'queued' || op.state === 'running') {
    children.push(h('p', { class: 'muted', text: op.state === 'queued' ? 'Waiting to start…' : 'Running…' }));
  } else if (op.state === 'reconciling') {
    children.push(banner('warn', 'Outcome unknown.', 'The command\'s result could not be confirmed. It will not be re-run automatically; check the container before running it again.'));
  }
  if (op.error) children.push(banner('bad', `${op.error.code}:`, op.error.message));

  if (res) {
    if (res.outcome === 'timed_out') children.push(banner('warn', 'Timed out.', 'The command exceeded the time limit. Processes it started in the background may still be running.'));
    if (res.outcome === 'unknown') children.push(banner('warn', 'Unknown outcome.', 'The command may or may not have completed. It will not be re-run automatically.'));
    children.push(outputBlock('stdout', res.stdout, res.stdout_truncated));
    children.push(outputBlock('stderr', res.stderr, res.stderr_truncated));
  } else if (isTerminal(op.state) && op.state === 'succeeded') {
    children.push(h('p', { class: 'muted', text: 'The result is no longer available (results expire after 15 minutes and are visible only to the person who ran the command).' }));
  }
  replace(r.node, ...children);
}

function outcomeBadge(outcome: string, exit: number | null): HTMLElement {
  if (outcome === 'completed') return badge(`exit ${exit ?? '?'}`, exit === 0 ? 'ok' : 'bad');
  if (outcome === 'timed_out') return badge('timed out', 'warn');
  return badge('outcome unknown', 'warn');
}

function outputBlock(label: string, text: string, truncated: boolean): HTMLElement {
  const empty = !text;
  return h('div', { class: 'field' },
    h('span', { class: 'row' }, h('strong', { text: label }), truncated ? badge('truncated', 'warn', 'Output exceeded the size limit and was cut off') : null),
    h('pre', { class: `output ${label === 'stderr' ? 'stderr' : ''} ${empty ? 'empty' : ''}`.trim(), attrs: { tabindex: 0, 'aria-label': label }, text: empty ? '(no output)' : text }));
}

function execUnavailableReason(c: ContainerDetail): string {
  if (!c.managed) return 'Commands are disabled for unmanaged containers.';
  if (c.safety && c.safety !== 'safe') return 'Commands are disabled because this container fails the safety policy.';
  if (c.metrics?.state && c.metrics.state !== 'Running') return `Commands need a running container (current state: ${c.metrics.state}).`;
  return 'You do not have permission to run commands in this container.';
}
