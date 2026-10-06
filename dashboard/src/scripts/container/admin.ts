// Admin-only sections of the container page: lifecycle, limits, access, owner, delete.
// The API enforces every rule; the UI mirrors them to avoid surprises.

import { ApiError, Submission, api, enc, qs } from '../../lib/api';
import { banner, byId, confirmDialog, h, replace, setBusy } from '../../lib/dom';
import { GiB, MiB, bytes, cores, pct } from '../../lib/format';
import { asOperation } from '../../lib/ops';
import { errorBlock } from '../../lib/shell';
import { controllable } from '../../lib/status';
import type { ContainerDetail, UserRow } from '../../lib/types';
import { opActive, type ViewCtx } from './context';

type Action = 'start' | 'stop' | 'restart' | 'freeze' | 'unfreeze';

export function setupAdmin(ctx: ViewCtx): void {
  const area = byId('admin-area');
  area.hidden = false;
  const c = ctx.current();
  const disabledNote = !c.capabilities.can_manage || !controllable(c)
    ? banner('warn', 'Controls disabled.', 'This container is unmanaged, unsafe or not active, so lifecycle, limit and access changes are disabled.')
    : null;
  if (!c.managed && c.status === 'active') {
    // Unmanaged containers can only be adopted; every other control stays disabled.
    replace(area, section('Adopt into management', 'adopt', adoptPanel(ctx)));
    return;
  }
  replace(area,
    section('Lifecycle', 'life', disabledNote, lifecycle(ctx)),
    section('Resource limits', 'limits', limitsForm(ctx)),
    section('Access', 'access', accessPanel(ctx)),
    section('Danger zone', 'danger', deletePanel(ctx)));
}

function section(title: string, key: string, ...children: (HTMLElement | null)[]): HTMLElement {
  return h('section', { class: 'section', attrs: { 'aria-labelledby': `adm-${key}` } }, h('h2', { attrs: { id: `adm-${key}` }, text: title }), ...children);
}

const canManage = (c: ContainerDetail) => c.capabilities.can_manage && controllable(c);

/** Show an accepted operation, or refresh if the response carried none. */
function accepted(ctx: ViewCtx, res: unknown): void {
  const op = asOperation(res);
  if (op) ctx.track(op); else ctx.refresh();
}

// ---------------------------------------------------------------- lifecycle

function lifecycle(ctx: ViewCtx): HTMLElement {
  const box = h('div');
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const sub = new Submission();
  const buttons: Record<Action, HTMLButtonElement> = {
    start: h('button', { text: 'Start', attrs: { type: 'button' } }),
    stop: h('button', { text: 'Stop', attrs: { type: 'button' } }),
    restart: h('button', { text: 'Restart', attrs: { type: 'button' } }),
    freeze: h('button', { text: 'Freeze', attrs: { type: 'button' } }),
    unfreeze: h('button', { text: 'Unfreeze', attrs: { type: 'button' } }),
  };
  let busy = false;

  const allowed = (state: string | null): Action[] => {
    switch ((state ?? '').toLowerCase()) {
      case 'running': return ['stop', 'restart', 'freeze'];
      case 'stopped': return ['start'];
      case 'frozen': return ['unfreeze', 'stop'];
      default: return ['start', 'stop'];
    }
  };

  const sync = (c: ContainerDetail) => {
    const ok = allowed(c.metrics?.state ?? null);
    for (const [a, b] of Object.entries(buttons) as [Action, HTMLButtonElement][]) {
      b.hidden = !ok.includes(a);
      b.disabled = busy || !canManage(c) || opActive(c);
    }
    if (opActive(c) && !busy) replace(msg, h('p', { class: 'small muted', text: 'Another operation is in progress; actions are available when it finishes.' }));
    else if (!busy) replace(msg);
  };

  for (const [action, btn] of Object.entries(buttons) as [Action, HTMLButtonElement][]) {
    btn.addEventListener('click', async () => {
      const c = ctx.current();
      let confirmEphemeral = false;
      if (action === 'stop' && c.ephemeral) {
        const ack = h('input', { attrs: { type: 'checkbox', id: 'ack-eph' } });
        const okd = await confirmDialog({
          title: `Stop ephemeral container ${c.name}?`,
          body: [h('p', { text: 'This container is ephemeral: LXD deletes it, including its data, when it stops.' })],
          controls: [h('label', { class: 'check', attrs: { for: 'ack-eph' } }, ack, 'I understand that stopping may permanently delete this container.')],
          isValid: () => ack.checked,
          confirmLabel: 'Stop and possibly delete',
          danger: true,
        });
        if (!okd) return;
        confirmEphemeral = true;
      } else if (action === 'stop' || action === 'restart') {
        const okd = await confirmDialog({
          title: `${action === 'stop' ? 'Stop' : 'Restart'} ${c.name}?`,
          body: [h('p', { text: 'Running processes in the container will be terminated.' })],
          confirmLabel: action === 'stop' ? 'Stop' : 'Restart',
        });
        if (!okd) return;
      }
      const body = { action, confirm_ephemeral: confirmEphemeral };
      busy = true;
      sync(c);
      setBusy(btn, true, 'Requesting…');
      replace(msg);
      try {
        const res = await api.post<unknown>(`/api/containers/${enc(ctx.id)}/actions`, body, { idempotencyKey: sub.keyFor(body) });
        sub.settle();
        accepted(ctx, res);
      } catch (err) {
        sub.settle(err);
        replace(msg, errorBlock(err, `Could not ${action}`));
      } finally {
        busy = false;
        setBusy(btn, false);
        sync(ctx.current());
      }
    });
  }

  ctx.onUpdate(sync);
  sync(ctx.current());
  replace(box, h('div', { class: 'row' }, ...Object.values(buttons)), msg);
  return box;
}

// ---------------------------------------------------------------- limits

function numberField(id: string, label: string, hint: string, attrs: Record<string, string | number>): { wrap: HTMLElement; input: HTMLInputElement; err: HTMLElement; hint: HTMLElement } {
  const input = h('input', { attrs: { id, type: 'number', inputmode: 'decimal', required: true, 'aria-describedby': `${id}-hint ${id}-err`, ...attrs } });
  const err = h('span', { class: 'err', attrs: { id: `${id}-err` } });
  const hintEl = h('span', { class: 'hint', attrs: { id: `${id}-hint` }, text: hint });
  const wrap = h('div', { class: 'field' }, h('label', { attrs: { for: id }, text: label }), input, hintEl, err);
  return { wrap, input, err, hint: hintEl };
}

function limitsForm(ctx: ViewCtx): HTMLElement {
  const coresF = numberField('lim-cores', 'CPU cores', 'Logical CPU slots counted against the owner quota.', { min: 1, step: 1 });
  const allowF = numberField('lim-allow', 'CPU allowance (%)', 'Hard cap: cores × % of CPU time per 100 ms.', { min: 10, max: 100, step: 1 });
  const memF = numberField('lim-mem', 'Memory (MiB)', 'Hard limit. Reductions must stay above current use + 64 MiB.', { min: 1, step: 1 });
  const diskF = numberField('lim-disk', 'Disk (GiB)', 'Expansion only: disk cannot be shrunk.', { min: 0, step: 'any' });
  const ack = h('input', { attrs: { type: 'checkbox', id: 'lim-ack' } });
  const ackWrap = h('label', { class: 'check', attrs: { for: 'lim-ack' } }, ack,
    'I understand that reducing memory on a running container can make processes fail if they need more than the new limit.');
  ackWrap.hidden = true;
  const save = h('button', { class: 'primary', text: 'Save limits', attrs: { type: 'submit' } });
  const reset = h('button', { text: 'Reset', attrs: { type: 'button' } });
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const changedNote = h('div');
  const form = h('form', { attrs: { novalidate: true } },
    h('div', { class: 'inline-inputs' }, coresF.wrap, allowF.wrap, memF.wrap, diskF.wrap),
    ackWrap, changedNote, h('div', { class: 'row' }, save, reset), msg);

  const sub = new Submission();
  let base = { version: 0, cpu_cores: 0, cpu_allowance_pct: 0, memory_bytes: 0, disk_bytes: 0 };
  let shown = { mem: '', disk: '' };
  let dirty = false;

  const fill = (c: ContainerDetail) => {
    const l = c.limits;
    base = {
      version: c.version,
      cpu_cores: l?.cpu_cores ?? 1,
      cpu_allowance_pct: l?.cpu_allowance_pct ?? 100,
      memory_bytes: l?.memory_bytes ?? 0,
      disk_bytes: l?.disk_bytes ?? 0,
    };
    coresF.input.value = String(base.cpu_cores);
    allowF.input.value = String(base.cpu_allowance_pct);
    shown = { mem: String(Math.round(base.memory_bytes / MiB)), disk: String(Number((base.disk_bytes / GiB).toFixed(2))) };
    memF.input.value = shown.mem;
    diskF.input.value = shown.disk;
    diskF.input.min = shown.disk;
    // Upper bounds from the server (owner quota + host capacity); the API re-validates anyway.
    const b = c.limit_bounds;
    const pool = l?.pool ?? '';
    if (b) {
      coresF.input.max = String(b.cpu_cores.max);
      memF.input.max = String(Math.floor(b.memory_bytes.max / MiB));
      const db = b.disk_bytes[pool];
      if (db) diskF.input.max = String(Number((db.max / GiB).toFixed(2)));
      coresF.hint.textContent = `Up to ${b.cpu_cores.max} with the owner's remaining quota and host capacity.`;
      memF.hint.textContent = `Up to ${Math.floor(b.memory_bytes.max / MiB)} MiB. Reductions must stay above current use + 64 MiB.`;
      if (db) diskF.hint.textContent = `Expansion only, up to ${Number((db.max / GiB).toFixed(2))} GiB.`;
    }
    ack.checked = false;
    dirty = false;
    replace(changedNote);
    for (const f of [coresF, allowF, memF, diskF]) { f.err.textContent = ''; f.input.removeAttribute('aria-invalid'); }
    validate();
  };

  // Unchanged display values keep the exact committed byte counts.
  const memBytes = () => (memF.input.value === shown.mem ? base.memory_bytes : Math.round(Number(memF.input.value)) * MiB);
  const diskBytes = () => (diskF.input.value === shown.disk ? base.disk_bytes : Math.round(Number(diskF.input.value) * 1024) * MiB);

  const setErr = (f: { input: HTMLInputElement; err: HTMLElement }, text: string) => {
    f.err.textContent = text;
    if (text) f.input.setAttribute('aria-invalid', 'true'); else f.input.removeAttribute('aria-invalid');
  };

  function validate(): boolean {
    const cpu = Number(coresF.input.value), al = Number(allowF.input.value), mem = Number(memF.input.value), disk = Number(diskF.input.value);
    setErr(coresF, Number.isInteger(cpu) && cpu >= 1 ? '' : 'Whole number, at least 1.');
    setErr(allowF, Number.isInteger(al) && al >= 10 && al <= 100 ? '' : 'Whole number from 10 to 100.');
    setErr(memF, Number.isInteger(mem) && mem >= 1 ? '' : 'Whole number of MiB.');
    setErr(diskF, Number.isFinite(disk) && disk > 0 ? (diskBytes() < base.disk_bytes ? `Disk can only grow (currently ${bytes(base.disk_bytes)}).` : '') : 'Enter a size in GiB.');
    const reducing = memF.err.textContent === '' && memBytes() < base.memory_bytes;
    ackWrap.hidden = !reducing;
    const valid = [coresF, allowF, memF, diskF].every((f) => !f.err.textContent);
    const c = ctx.current();
    save.disabled = !valid || !canManage(c) || opActive(c) || (reducing && !ack.checked);
    return valid;
  }

  form.addEventListener('input', () => { dirty = true; validate(); });
  reset.addEventListener('click', () => { fill(ctx.current()); replace(msg); });

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!validate()) return;
    const body = {
      version: base.version,
      cpu_cores: Number(coresF.input.value),
      cpu_allowance_pct: Number(allowF.input.value),
      memory_bytes: memBytes(),
      disk_bytes: diskBytes(),
      confirm_reduction: memBytes() < base.memory_bytes && ack.checked,
    };
    const summary = `${cores(body.cpu_cores)} at ${pct(body.cpu_allowance_pct, 0)}, ${bytes(body.memory_bytes)} memory, ${bytes(body.disk_bytes)} disk`;
    const okd = await confirmDialog({
      title: 'Apply new limits?',
      body: [h('p', { text: summary }),
        body.disk_bytes > base.disk_bytes ? h('p', { class: 'small', text: 'Disk growth cannot be undone.' }) : null,
        h('p', { class: 'small muted', text: 'Increases are checked against the owner quota and host budget.' })],
      confirmLabel: 'Apply',
    });
    if (!okd) return;
    setBusy(save, true, 'Saving…');
    replace(msg);
    try {
      const res = await api.patch<unknown>(`/api/containers/${enc(ctx.id)}/limits`, body, { idempotencyKey: sub.keyFor(body) });
      sub.settle();
      dirty = false;
      replace(msg, banner('ok', 'Limit change accepted.', 'Values update when the operation completes.'));
      accepted(ctx, res);
    } catch (err) {
      sub.settle(err);
      const extra = err instanceof ApiError && err.code === 'VERSION_CONFLICT'
        ? h('p', null, 'The container changed since this form was loaded. ', h('button', { class: 'small', text: 'Load current values', attrs: { type: 'button' }, on: { click: () => { fill(ctx.current()); ctx.refresh(); replace(msg); } } }))
        : null;
      replace(msg, errorBlock(err, 'Limits not changed'), extra);
      if (err instanceof ApiError) {
        const map: Record<string, { input: HTMLInputElement; err: HTMLElement }> = { cpu_cores: coresF, cpu_allowance_pct: allowF, memory_bytes: memF, disk_bytes: diskF };
        for (const [k, v] of Object.entries(err.fields)) if (map[k]) setErr(map[k]!, v);
      }
    } finally {
      setBusy(save, false);
      validate();
    }
  });

  fill(ctx.current());
  ctx.onUpdate((c) => {
    if (!dirty) { fill(c); return; }
    if (c.version !== base.version) {
      replace(changedNote, banner('warn', 'Limits changed elsewhere.', `The container is now at version ${c.version} (this form was loaded at ${base.version}). Reset to load the current values.`));
    }
    validate();
  });
  return form;
}

// ---------------------------------------------------------------- access & owner

function accessPanel(ctx: ViewCtx): HTMLElement {
  const box = h('div');
  const list = h('ul', { class: 'plain-list' });
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const addSel = h('select', { attrs: { id: 'acc-add' } });
  const addBtn = h('button', { text: 'Give access', attrs: { type: 'submit' } });
  const ownerSel = h('select', { attrs: { id: 'acc-owner' } });
  const ownerBtn = h('button', { text: 'Transfer ownership', attrs: { type: 'submit' } });
  const addForm = h('form', { class: 'field' },
    h('label', { attrs: { for: 'acc-add' }, text: 'Add a user' }),
    h('div', { class: 'row' }, addSel, addBtn),
    h('span', { class: 'hint', text: 'Assigned users can view this container and run commands as the unprivileged user. Their quota is not charged.' }));
  const ownerForm = h('form', { class: 'field' },
    h('label', { attrs: { for: 'acc-owner' }, text: 'Owner (quota is charged to the owner)' }),
    h('div', { class: 'row' }, ownerSel, ownerBtn),
    h('span', { class: 'hint', text: 'Transfer moves this container’s allocation to the new owner’s quota; it fails if their quota is insufficient.' }));
  replace(box, h('h3', { text: 'Users with access' }), list, addForm, ownerForm, msg);

  let users: UserRow[] = [];
  let busy = false;
  const sub = new Submission();

  async function mutate(label: string, fn: (key: (b: unknown) => string) => Promise<unknown>, btn?: HTMLButtonElement): Promise<void> {
    busy = true;
    if (btn) setBusy(btn, true);
    render(ctx.current());
    replace(msg);
    try {
      const res = await fn((b) => sub.keyFor(b));
      sub.settle();
      replace(msg, banner('ok', `${label}.`));
      accepted(ctx, res);
    } catch (err) {
      sub.settle(err);
      replace(msg, errorBlock(err, `${label} failed`));
    } finally {
      busy = false;
      if (btn) setBusy(btn, false);
      render(ctx.current());
    }
  }

  function render(c: ContainerDetail): void {
    const access = c.access ?? [];
    const disabled = busy || !canManage(c);
    replace(list, ...(access.length ? access.map((a) => h('li', null,
      h('span', { text: a.email }),
      h('button', {
        class: 'small danger', text: 'Remove', attrs: { type: 'button', disabled, 'aria-label': `Remove access for ${a.email}` },
        on: {
          click: async () => {
            const okd = await confirmDialog({ title: `Remove access for ${a.email}?`, body: [h('p', { text: 'They lose access to this container immediately, including any running command results.' })], confirmLabel: 'Remove access', danger: true });
            if (okd) void mutate('Access removed', (key) => api.del(`/api/containers/${enc(ctx.id)}/assignments/${enc(a.user_id)}`, { idempotencyKey: key({ remove: a.user_id }) }));
          },
        },
      }))) : [h('li', { class: 'muted', text: 'Only the owner and administrators have access.' })]));

    const active = users.filter((u) => u.status === 'active');
    const has = new Set(access.map((a) => a.user_id));
    const candidates = active.filter((u) => !has.has(u.id) && u.id !== c.owner?.id);
    const prevAdd = addSel.value;
    replace(addSel, h('option', { attrs: { value: '' }, text: candidates.length ? 'Choose a user…' : 'No other active users' }),
      ...candidates.map((u) => h('option', { attrs: { value: u.id }, text: u.email })));
    if (candidates.some((u) => u.id === prevAdd)) addSel.value = prevAdd;
    addSel.disabled = disabled || !candidates.length;
    addBtn.disabled = disabled || !addSel.value;

    const prevOwner = ownerSel.value;
    replace(ownerSel, ...active.map((u) => h('option', { attrs: { value: u.id }, text: u.id === c.owner?.id ? `${u.email} (current)` : u.email })));
    if (!active.some((u) => u.id === c.owner?.id) && c.owner) ownerSel.prepend(h('option', { attrs: { value: c.owner.id }, text: `${c.owner.email} (current)` }));
    ownerSel.value = prevOwner && prevOwner !== '' && [...ownerSel.options].some((o) => o.value === prevOwner) ? prevOwner : c.owner?.id ?? '';
    ownerSel.disabled = disabled;
    ownerBtn.disabled = disabled || !ownerSel.value || ownerSel.value === c.owner?.id;
  }

  addSel.addEventListener('change', () => { addBtn.disabled = busy || !addSel.value; });
  ownerSel.addEventListener('change', () => { ownerBtn.disabled = busy || !ownerSel.value || ownerSel.value === ctx.current().owner?.id; });

  addForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const uid = addSel.value;
    if (!uid) return;
    void mutate('Access granted', (key) => api.put(`/api/containers/${enc(ctx.id)}/assignments/${enc(uid)}`, undefined, { idempotencyKey: key({ add: uid }) }), addBtn);
  });

  ownerForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const uid = ownerSel.value;
    const c = ctx.current();
    const target = users.find((u) => u.id === uid);
    if (!uid || uid === c.owner?.id || !target) return;
    const l = c.limits;
    const okd = await confirmDialog({
      title: `Transfer ${c.name} to ${target.email}?`,
      body: [h('p', { text: `${target.email} becomes the owner and their quota is charged ${l ? `${cores(l.cpu_cores)}, ${bytes(l.memory_bytes)} memory and ${bytes(l.disk_bytes)} disk` : 'this container’s allocation'}. The previous owner’s quota is released.` })],
      confirmLabel: 'Transfer',
    });
    if (!okd) return;
    const body = { owner_id: uid };
    void mutate('Ownership transferred', (key) => api.patch(`/api/containers/${enc(ctx.id)}/owner`, body, { idempotencyKey: key(body) }), ownerBtn);
  });

  api.get<{ users: UserRow[] }>('/api/users').then((r) => { users = r.users; render(ctx.current()); })
    .catch((err) => replace(msg, errorBlock(err, 'Could not load users')));
  ctx.onUpdate((c) => { if (!busy) render(c); });
  render(ctx.current());
  return box;
}

// ---------------------------------------------------------------- delete

function deletePanel(ctx: ViewCtx): HTMLElement {
  const btn = h('button', { class: 'danger', text: 'Delete container…', attrs: { type: 'button' } });
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const sub = new Submission();
  const sync = (c: ContainerDetail) => { btn.disabled = !canManage(c) || opActive(c); };

  btn.addEventListener('click', async () => {
    const c = ctx.current();
    const input = h('input', { attrs: { type: 'text', id: 'del-name', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'del-hint' } });
    const okd = await confirmDialog({
      title: `Delete ${c.name}?`,
      body: [h('p', { text: 'The container and all of its data are permanently deleted. Its allocation is released after LXD confirms the deletion.' })],
      controls: [h('div', { class: 'field' },
        h('label', { attrs: { for: 'del-name' } }, 'Type the container name to confirm: ', h('code', { text: c.name })),
        input,
        h('span', { class: 'hint', attrs: { id: 'del-hint' }, text: 'This cannot be undone.' }))],
      isValid: () => input.value === c.name,
      confirmLabel: 'Delete permanently',
      danger: true,
    });
    if (!okd || input.value !== c.name) return;
    setBusy(btn, true, 'Deleting…');
    replace(msg);
    const key = sub.keyFor({ delete: c.id, name: c.name });
    try {
      const res = await api.del<unknown>(`/api/containers/${enc(ctx.id)}${qs({ confirm_name: c.name })}`, { idempotencyKey: key });
      sub.settle();
      replace(msg, banner('info', 'Deletion requested.', 'Waiting for LXD to confirm…'));
      accepted(ctx, res);
    } catch (err) {
      sub.settle(err);
      replace(msg, errorBlock(err, 'Not deleted'));
    } finally {
      setBusy(btn, false);
      sync(ctx.current());
    }
  });

  ctx.onUpdate(sync);
  sync(ctx.current());
  return h('div', null, h('p', { class: 'small muted', text: 'Deleting requires typing the container name.' }), btn, msg);
}

// ---------------------------------------------------------------- adoption

function adoptPanel(ctx: ViewCtx): HTMLElement {
  const c = ctx.current();
  const box = h('div');
  if (c.safety !== 'safe') {
    replace(box,
      banner('warn', 'Manual remediation required.',
        'This container fails the safety check, so it cannot be adopted. The app never changes the configuration of another workload for you.'),
      h('ul', {}, ...c.safety_reasons.map((r) => h('li', { text: r }))));
    return box;
  }
  const o = c.observed_limits;
  const owner = h('select', { attrs: { id: 'adopt-owner', required: true } });
  const coresF = numberField('adopt-cores', 'CPU cores', 'Charged to the owner quota.', { min: 1, step: 1, value: o?.cpu_cores ?? 1 });
  const allowF = numberField('adopt-allow', 'CPU allowance (%)', 'Hard cap: cores × % of CPU time per 100 ms.', { min: 10, max: 100, step: 1, value: 100 });
  const memF = numberField('adopt-mem', 'Memory (MiB)', 'Hard limit applied on adoption.', { min: 128, step: 1, value: o?.memory_bytes ? Math.round(o.memory_bytes / MiB) : 512 });
  const diskF = numberField('adopt-disk', 'Disk (GiB)', 'Root disk quota; cannot be smaller than the current size.', { min: 1, step: 'any', value: o?.disk_bytes ? Number((o.disk_bytes / GiB).toFixed(2)) : 4 });
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const submit = h('button', { class: 'primary', text: 'Adopt container', attrs: { type: 'submit' } });
  const sub = new Submission();
  const form = h('form', { attrs: { novalidate: true } },
    h('p', { class: 'small muted', text: 'Adoption writes an identity marker and the limits below to this container, provisions the unprivileged guest account and charges the allocation to the selected owner.' }),
    h('div', { class: 'field' }, h('label', { attrs: { for: 'adopt-owner' }, text: 'Owner' }), owner),
    coresF.wrap, allowF.wrap, memF.wrap, diskF.wrap, h('div', { class: 'row' }, submit), msg);
  api.get<{ users: UserRow[] }>('/api/users').then((r) => {
    for (const u of r.users.filter((x) => x.status !== 'revoked')) owner.append(h('option', { attrs: { value: u.id }, text: `${u.email} (${u.status})` }));
  }).catch((err) => replace(msg, errorBlock(err, 'Could not load users')));
  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      owner_id: owner.value,
      cpu_cores: Math.round(Number(coresF.input.value)),
      cpu_allowance_pct: Math.round(Number(allowF.input.value)),
      memory_bytes: Math.round(Number(memF.input.value)) * MiB,
      disk_bytes: Math.round(Number(diskF.input.value) * 1024) * MiB,
    };
    setBusy(submit, true, 'Requesting…');
    replace(msg);
    try {
      const res = await api.post<unknown>(`/api/containers/${enc(ctx.id)}/adopt`, body, { idempotencyKey: sub.keyFor(body) });
      sub.settle();
      accepted(ctx, res);
    } catch (err) {
      sub.settle(err);
      replace(msg, errorBlock(err, 'Could not adopt'));
    } finally {
      setBusy(submit, false);
    }
  });
  replace(box, form);
  return box;
}
