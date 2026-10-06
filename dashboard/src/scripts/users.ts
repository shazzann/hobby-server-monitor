// /users/ (admin): invitations, roles, quotas, revocation.

import { quotaText } from '../lib/alloc';
import { ApiError, api, enc } from '../lib/api';
import { badge, banner, byId, confirmDialog, h, kv, replace, setBusy, type Tone } from '../lib/dom';
import { GiB, MiB, bytes, cores, dateTime } from '../lib/format';
import { errorBlock, initPage, showContent, showLoadError } from '../lib/shell';
import type { Me, ResourceTriple, UserRow } from '../lib/types';

const STATUS_TONE: Record<string, Tone> = { active: 'ok', pending: 'warn', revoked: 'bad' };

let me: Me;
const openCards = new Set<string>();

async function main(): Promise<void> {
  const ctx = await initPage({ adminOnly: true, what: 'user management' });
  if (!ctx) return;
  me = ctx.me;
  setupInvite();
  try {
    await loadUsers();
    showContent();
  } catch (err) {
    showLoadError(err, 'user management');
  }
}

async function loadUsers(): Promise<void> {
  const res = await api.get<{ users: UserRow[] }>('/api/users');
  const users = [...res.users].sort((a, b) => statusOrder(a.status) - statusOrder(b.status) || a.email.localeCompare(b.email));
  byId('users-count').textContent = `${users.length} total`;
  replace(byId('users-list'), ...(users.length ? users.map(card) : [h('div', { class: 'state-box', text: 'No users yet.' })]));
}

async function reload(): Promise<void> {
  try { await loadUsers(); } catch (err) { replace(byId('users-msg'), errorBlock(err, 'Could not refresh the user list')); }
}

function statusOrder(s: string): number {
  return s === 'pending' ? 0 : s === 'active' ? 1 : 2;
}

// ---------------------------------------------------------------- invite

function setupInvite(): void {
  const form = byId<HTMLFormElement>('inv-form');
  const email = byId<HTMLInputElement>('inv-email');
  const role = byId<HTMLSelectElement>('inv-role');
  const coresIn = byId<HTMLInputElement>('inv-cores');
  const memIn = byId<HTMLInputElement>('inv-mem');
  const diskIn = byId<HTMLInputElement>('inv-disk');
  const btn = byId<HTMLButtonElement>('inv-submit');
  const msg = byId('inv-msg');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const errs: [HTMLInputElement, string, string][] = [
      [email, 'inv-email-err', /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.value.trim()) ? '' : 'Enter a valid email address.'],
      [coresIn, 'inv-cores-err', isWhole(coresIn.value) ? '' : 'Whole number of cores (0 or more).'],
      [memIn, 'inv-mem-err', isNonNeg(memIn.value) ? '' : 'Enter GiB (0 or more).'],
      [diskIn, 'inv-disk-err', isNonNeg(diskIn.value) ? '' : 'Enter GiB (0 or more).'],
    ];
    for (const [input, errId, text] of errs) {
      byId(errId).textContent = text;
      if (text) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid');
    }
    const bad = errs.find((x) => x[2]);
    if (bad) { bad[0].focus(); return; }

    const body = {
      email: email.value.trim(),
      role: role.value,
      quota: { cpu_cores: Number(coresIn.value), memory_bytes: gibToBytes(memIn.value), disk_bytes: gibToBytes(diskIn.value) },
    };
    setBusy(btn, true, 'Creating…');
    replace(msg);
    try {
      const res = await api.post<{ invitation: { id: string; email: string; expires_at: string }; user_id: string; link: string }>('/api/invitations', body);
      showLink(res.invitation.email, res.link, res.invitation.expires_at);
      form.reset();
      void reload();
    } catch (err) {
      replace(msg, errorBlock(err, 'Invitation not created'));
      if (err instanceof ApiError) {
        const map: Record<string, string> = { email: 'inv-email-err', 'quota.cpu_cores': 'inv-cores-err', 'quota.memory_bytes': 'inv-mem-err', 'quota.disk_bytes': 'inv-disk-err', cpu_cores: 'inv-cores-err', memory_bytes: 'inv-mem-err', disk_bytes: 'inv-disk-err' };
        for (const [k, v] of Object.entries(err.fields)) { const id = map[k]; if (id) byId(id).textContent = v; }
      }
    } finally {
      setBusy(btn, false);
    }
  });
}

function showLink(email: string, link: string, expires: string): void {
  const input = h('input', { attrs: { type: 'text', readonly: true, id: 'inv-link', 'aria-describedby': 'inv-link-note' } });
  input.value = link;
  const copyBtn = h('button', { text: 'Copy', attrs: { type: 'button' } });
  const status = h('span', { class: 'small', attrs: { 'aria-live': 'polite' } });
  copyBtn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(link);
      status.textContent = 'Copied.';
    } catch {
      input.select();
      status.textContent = 'Select the link and copy it manually (Ctrl+C).';
    }
  });
  input.addEventListener('focus', () => input.select());
  const dismiss = h('button', { class: 'small', text: 'I have saved the link, hide it', attrs: { type: 'button' } });
  const box = h('div', { class: 'banner ok', attrs: { role: 'status' } },
    h('p', null, h('span', { class: 'banner-title', text: 'Invitation created ' }), `for ${email}. Expires ${dateTime(expires)}.`),
    h('div', { class: 'field' },
      h('label', { attrs: { for: 'inv-link' }, text: 'One-time invitation link' }),
      h('div', { class: 'link-box' }, input, copyBtn),
      h('span', { class: 'hint', attrs: { id: 'inv-link-note' } },
        h('strong', { text: 'This link is shown only once. ' }),
        'Send it to the user over a trusted channel. It works once, only for this Google account, and cannot be displayed again; if it is lost, revoke the invitation and create a new one.'),
      status),
    dismiss);
  dismiss.addEventListener('click', () => { input.value = ''; replace(byId('inv-result')); });
  replace(byId('inv-result'), box);
  input.focus();
}

// ---------------------------------------------------------------- user cards

function card(u: UserRow): HTMLElement {
  const isMe = u.id === me.user.id;
  const head = h('header', null,
    h('span', { class: 'email', text: u.email }),
    u.display_name && u.display_name !== u.email ? h('span', { class: 'muted small', text: u.display_name }) : null,
    badge(u.status, STATUS_TONE[u.status] ?? 'neutral'),
    badge(u.role, u.role === 'admin' ? 'info' : 'neutral'),
    isMe ? badge('you', 'neutral') : null);

  const containers = u.containers.length
    ? h('span', null, ...u.containers.flatMap((c, i) => [
      i ? ', ' : '',
      h('a', { attrs: { href: `/containers/view/?id=${enc(c.id)}` }, text: c.name }),
      h('span', { class: 'muted', text: ` (${c.relation})` }),
    ]))
    : h('span', { class: 'muted', text: 'none' });

  const rows: [string, string | HTMLElement][] = [
    ['CPU', quotaText('cpu_cores', u.quota, u.allocated, u.pending)],
    ['Memory', quotaText('memory_bytes', u.quota, u.allocated, u.pending)],
    ['Disk', quotaText('disk_bytes', u.quota, u.allocated, u.pending)],
    ['Containers', containers],
  ];
  if (u.invitation) rows.push(['Invitation', `pending, expires ${dateTime(u.invitation.expires_at)}`]);

  const el = h('article', { class: 'user-card', attrs: { 'aria-label': u.email } }, head, kv(rows));
  if (u.status !== 'revoked') {
    const details = h('details', null, h('summary', { text: 'Manage role, quota and access' }), manage(u, isMe));
    details.open = openCards.has(u.id);
    details.addEventListener('toggle', () => { if (details.open) openCards.add(u.id); else openCards.delete(u.id); });
    el.appendChild(details);
  }
  return el;
}

function manage(u: UserRow, isMe: boolean): HTMLElement {
  const msg = h('div', { attrs: { 'aria-live': 'polite' } });
  const idp = `u-${u.id.slice(0, 8)}`;

  // role
  const roleSel = h('select', { attrs: { id: `${idp}-role` } },
    h('option', { attrs: { value: 'user' }, text: 'User' }),
    h('option', { attrs: { value: 'admin' }, text: 'Administrator' }));
  roleSel.value = u.role;
  const roleBtn = h('button', { text: 'Change role', attrs: { type: 'submit', disabled: true } });
  roleSel.addEventListener('change', () => { roleBtn.disabled = roleSel.value === u.role; });
  const roleForm = h('form', { class: 'field' },
    h('label', { attrs: { for: `${idp}-role` }, text: 'Role' }),
    h('div', { class: 'row' }, roleSel, roleBtn),
    isMe ? h('span', { class: 'hint', text: 'Demoting yourself removes your administrator access. The last active administrator cannot be demoted.' }) : null);
  roleForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const okd = await confirmDialog({
      title: `Change ${u.email} to ${roleSel.value === 'admin' ? 'administrator' : 'user'}?`,
      body: [h('p', { text: roleSel.value === 'admin' ? 'Administrators can manage every container, user and quota.' : 'They will only see containers they own or are assigned to.' })],
      confirmLabel: 'Change role',
    });
    if (!okd) return;
    await patchUser(u, { role: roleSel.value }, roleBtn, msg, 'Role changed');
  });

  // quota (unchanged display values keep exact byte counts)
  const shown = { cores: String(u.quota.cpu_cores ?? 0), mem: gibStr(u.quota.memory_bytes), disk: gibStr(u.quota.disk_bytes) };
  const qIn = (key: string, label: string, val: string, step: string) => {
    const input = h('input', { attrs: { id: `${idp}-${key}`, type: 'number', min: 0, step, required: true } });
    input.value = val;
    return { input, wrap: h('div', { class: 'field' }, h('label', { attrs: { for: `${idp}-${key}` }, text: label }), input) };
  };
  const qc = qIn('qc', 'CPU cores', shown.cores, '1');
  const qm = qIn('qm', 'Memory (GiB)', shown.mem, '0.25');
  const qd = qIn('qd', 'Disk (GiB)', shown.disk, '1');
  const qBtn = h('button', { text: 'Save quota', attrs: { type: 'submit' } });
  const quotaForm = h('form', { attrs: { novalidate: true } },
    h('fieldset', null, h('legend', { text: 'Quota' }),
      h('div', { class: 'inline-inputs' }, qc.wrap, qm.wrap, qd.wrap),
      h('p', { class: 'hint small', text: `Currently committed: ${allocText(u)}. A quota cannot go below current allocations plus pending reservations.` }),
      h('div', { class: 'row' }, qBtn)));
  quotaForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!isWhole(qc.input.value) || !isNonNeg(qm.input.value) || !isNonNeg(qd.input.value)) {
      replace(msg, banner('warn', 'Enter whole CPU cores and non-negative GiB values.'));
      return;
    }
    const quota: ResourceTriple = {
      cpu_cores: Number(qc.input.value),
      memory_bytes: qm.input.value === shown.mem ? u.quota.memory_bytes : gibToBytes(qm.input.value),
      disk_bytes: qd.input.value === shown.disk ? u.quota.disk_bytes : gibToBytes(qd.input.value),
    };
    await patchUser(u, { quota }, qBtn, msg, 'Quota saved');
  });

  // revoke
  const actions = h('div', { class: 'row' });
  if (u.invitation && u.status === 'pending') {
    const b = h('button', { class: 'danger small', text: 'Revoke invitation', attrs: { type: 'button' } });
    b.addEventListener('click', async () => {
      const okd = await confirmDialog({ title: `Revoke the invitation for ${u.email}?`, body: [h('p', { text: 'The invitation link stops working immediately.' })], confirmLabel: 'Revoke invitation', danger: true });
      if (!okd) return;
      await run(b, msg, 'Invitation revoked', () => api.del(`/api/invitations/${enc(u.invitation!.id)}`));
    });
    actions.appendChild(b);
  }
  if (u.status === 'active') {
    const b = h('button', { class: 'danger small', text: 'Revoke access', attrs: { type: 'button', disabled: isMe, title: isMe ? 'You cannot revoke your own account here' : undefined } });
    b.addEventListener('click', async () => {
      const ack = h('input', { attrs: { type: 'checkbox', id: `${idp}-ack` } });
      const okd = await confirmDialog({
        title: `Revoke access for ${u.email}?`,
        body: [h('p', { text: 'All of their sessions end immediately and they can no longer sign in. Containers they own keep running and remain charged to them until reassigned or deleted.' })],
        controls: [h('label', { class: 'check', attrs: { for: `${idp}-ack` } }, ack, 'I understand this signs them out everywhere.')],
        isValid: () => ack.checked,
        confirmLabel: 'Revoke access',
        danger: true,
      });
      if (!okd) return;
      await run(b, msg, 'Access revoked', () => api.post(`/api/users/${enc(u.id)}/revoke`));
    });
    actions.appendChild(b);
  }

  return h('div', null, roleForm, quotaForm, actions.childElementCount ? actions : null, msg);
}

async function patchUser(u: UserRow, body: Record<string, unknown>, btn: HTMLButtonElement, msg: HTMLElement, done: string): Promise<void> {
  await run(btn, msg, done, () => api.patch(`/api/users/${enc(u.id)}`, body), (err) => {
    if (err.code === 'QUOTA_BELOW_ALLOCATION') {
      return h('p', { class: 'small', text: 'The new quota is below what this user already has allocated or reserved. Reduce or transfer their containers first, or choose a higher quota.' });
    }
    if (err.code === 'LAST_ADMIN') {
      return h('p', { class: 'small', text: 'At least one active administrator must remain. Promote someone else first.' });
    }
    return null;
  });
}

async function run(btn: HTMLButtonElement, msg: HTMLElement, done: string, fn: () => Promise<unknown>, explain?: (err: ApiError) => HTMLElement | null): Promise<void> {
  setBusy(btn, true);
  replace(msg);
  try {
    await fn();
    replace(byId('users-msg'), banner('ok', `${done}.`));
    await reload();
  } catch (err) {
    replace(msg, errorBlock(err, 'Not saved'), err instanceof ApiError && explain ? explain(err) : null);
    setBusy(btn, false);
  }
}

function allocText(u: UserRow): string {
  const sum = (k: keyof ResourceTriple) => (u.allocated[k] ?? 0) + (u.pending[k] ?? 0);
  return `${cores(sum('cpu_cores'))}, ${bytes(sum('memory_bytes'))} memory, ${bytes(sum('disk_bytes'))} disk (allocated + pending)`;
}

function isWhole(v: string): boolean { return /^\d+$/.test(v.trim()); }
function isNonNeg(v: string): boolean { const n = Number(v); return v.trim() !== '' && Number.isFinite(n) && n >= 0; }
/** GiB (decimal input) -> bytes, rounded to whole MiB. */
function gibToBytes(v: string): number { return Math.round(Number(v) * 1024) * MiB; }
function gibStr(b: number | null): string { return b === null ? '0' : String(Number((b / GiB).toFixed(2))); }

void main().catch((err: unknown) => showLoadError(err, 'user management'));
