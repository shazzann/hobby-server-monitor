// /containers/new/ (admin): discovered options + live bounds from /api/creation-options.

import { ApiError, Submission, api, qs } from '../lib/api';
import { banner, byId, confirmDialog, h, kv, replace, setBusy } from '../lib/dom';
import { GiB, MiB, bytes, cores, pct, yesNo } from '../lib/format';
import { errorBlock, initPage, showContent, showLoadError } from '../lib/shell';
import type { Bound, CreationBounds, CreationOptions, Operation } from '../lib/types';

type Key = 'cores' | 'allow' | 'mem' | 'disk';

interface Slider {
  key: Key;
  range: HTMLInputElement;
  input: HTMLInputElement;
  err: HTMLElement;
  minEl: HTMLElement;
  maxEl: HTMLElement;
  min: number;
  max: number;
  /** UI unit -> bytes or plain value sent to the API. */
  toApi: (v: number) => number;
  label: (v: number) => string;
}

const FIELD_TO_KEY: Record<string, string> = {
  name: 'name', image: 'image', pool: 'pool', network: 'network', owner_id: 'owner', description: 'desc',
  cpu_cores: 'cores', cpu_allowance_pct: 'allow', memory_bytes: 'mem', disk_bytes: 'disk',
};

const DEFAULTS: Record<Key, number> = { cores: 1, allow: 100, mem: 512, disk: 4 };

async function main(): Promise<void> {
  const ctx = await initPage({ adminOnly: true, what: 'container creation' });
  if (!ctx) return;

  const form = byId<HTMLFormElement>('new-form');
  const owner = byId<HTMLSelectElement>('f-owner');
  const name = byId<HTMLInputElement>('f-name');
  const image = byId<HTMLSelectElement>('f-image');
  const pool = byId<HTMLSelectElement>('f-pool');
  const network = byId<HTMLSelectElement>('f-network');
  const autostart = byId<HTMLInputElement>('f-autostart');
  const ephemeral = byId<HTMLInputElement>('f-ephemeral');
  const desc = byId<HTMLTextAreaElement>('f-desc');
  const submit = byId<HTMLButtonElement>('f-submit');
  const banners = byId('new-banners');
  const submitMsg = byId('submit-msg');

  const mk = (key: Key, toApi: (v: number) => number, label: (v: number) => string): Slider => {
    const bounds = byId(`f-${key}-bounds`);
    return {
      key, toApi, label,
      range: byId<HTMLInputElement>(`f-${key}-range`),
      input: byId<HTMLInputElement>(`f-${key}`),
      err: byId(`f-${key}-err`),
      minEl: bounds.querySelector<HTMLElement>('[data-min]')!,
      maxEl: bounds.querySelector<HTMLElement>('[data-max]')!,
      min: 0, max: 0,
    };
  };
  const sliders: Record<Key, Slider> = {
    cores: mk('cores', (v) => v, (v) => cores(v)),
    allow: mk('allow', (v) => v, (v) => pct(v, 0)),
    mem: mk('mem', (v) => v * MiB, (v) => bytes(v * MiB)),
    disk: mk('disk', (v) => v * GiB, (v) => bytes(v * GiB)),
  };

  let opts: CreationOptions | null = null;
  let nameRe: RegExp | null = null;
  let loadSeq = 0;
  let first = true;
  const touched = new Set<string>();
  const submission = new Submission();
  let submitting = false;

  // ---------------------------------------------------------------- options & bounds

  async function loadOptions(ownerId: string): Promise<void> {
    const seq = ++loadSeq;
    submit.disabled = true;
    replace(banners, h('p', { class: 'muted', text: 'Loading options…' }));
    try {
      const o = await api.get<CreationOptions>(`/api/creation-options${qs({ owner_id: ownerId })}`);
      if (seq !== loadSeq) return;
      opts = o;
      applyOptions(o, ownerId);
      // Bounds are per owner: if the requested owner is not eligible, reload for the one now selected.
      if (owner.value && owner.value !== ownerId) { void loadOptions(owner.value); return; }
      if (first) { first = false; showContent(); name.focus(); }
    } catch (err) {
      if (seq !== loadSeq) return;
      if (first) { showLoadError(err, 'container creation'); return; }
      replace(banners, errorBlock(err, 'Could not load creation options'));
    }
  }

  function fillSelect<T>(sel: HTMLSelectElement, items: T[], value: (t: T) => string, text: (t: T) => string, empty: string): void {
    const prev = sel.value;
    replace(sel, ...(items.length ? items.map((t) => h('option', { attrs: { value: value(t) }, text: text(t) })) : [h('option', { attrs: { value: '' }, text: empty })]));
    if (items.some((t) => value(t) === prev)) sel.value = prev;
    sel.disabled = !items.length;
  }

  function applyOptions(o: CreationOptions, ownerId: string): void {
    fillSelect(owner, o.owners, (u) => u.id, (u) => (u.id === ctx!.me.user.id ? `${u.email} (you)` : u.email), 'No eligible owners');
    if (o.owners.some((u) => u.id === ownerId)) owner.value = ownerId;
    fillSelect(image, o.images, (i) => i.alias, (i) => [i.description || i.alias, i.architecture ? `(${i.architecture})` : ''].join(' ').trim(), 'No safe images discovered');
    fillSelect(pool, o.pools, (p) => p.name, (p) => `${p.name} (${p.driver}${p.quota_capable ? '' : ', no disk quota'})`, 'No usable storage pools');
    fillSelect(network, o.networks, (n) => n.name, (n) => n.name, 'No safe networks discovered');
    try { nameRe = new RegExp(o.name_rule); } catch { nameRe = null; }
    applyBounds(o.bounds);
    renderBanners();
    validateAll();
  }

  function setBound(s: Slider, b: Bound | undefined, conv: (v: number) => number, convMax: (v: number) => number, step = 1): void {
    s.min = b ? conv(b.min) : 0;
    s.max = b ? convMax(b.max) : 0;
    const available = Boolean(b) && s.max >= s.min;
    for (const el of [s.range, s.input]) {
      el.min = String(s.min);
      el.max = String(Math.max(s.min, s.max));
      el.step = String(step);
      el.disabled = !available;
    }
    s.minEl.textContent = `min ${s.label(s.min)}`;
    s.maxEl.textContent = available ? `max ${s.label(s.max)}` : 'none available';
    if (!s.input.value) s.input.value = String(Math.min(Math.max(DEFAULTS[s.key], s.min), Math.max(s.min, s.max)));
    s.range.value = s.input.value;
  }

  function applyBounds(b: CreationBounds): void {
    setBound(sliders.cores, b.cpu_cores, Math.ceil, Math.floor);
    setBound(sliders.allow, b.cpu_allowance_pct, Math.ceil, Math.floor);
    const memStep = Math.max(1, Math.round((b.memory_bytes.step ?? MiB) / MiB));
    setBound(sliders.mem, b.memory_bytes, (v) => Math.ceil(v / MiB), (v) => Math.floor(v / MiB), memStep);
    setBound(sliders.disk, b.disk_bytes[pool.value], (v) => Math.ceil(v / GiB), (v) => Math.floor(v / GiB));
    const p = opts?.pools.find((x) => x.name === pool.value);
    byId('f-pool-hint').textContent = p
      ? `${p.driver}${p.quota_capable ? ', enforces disk quotas' : ', cannot enforce disk quotas'}; ${p.total_bytes ? `${bytes(p.used_bytes)} of ${bytes(p.total_bytes)} physically used` : 'capacity unknown'}.`
      : '';
  }

  function renderBanners(): void {
    const nodes: HTMLElement[] = [];
    const blocked = opts?.bounds.blocked_reason;
    if (blocked) nodes.push(banner('bad', 'Creation is blocked.', blocked));
    for (const s of Object.values(sliders)) {
      if (s.max < s.min) nodes.push(banner('warn', 'Insufficient capacity.', `${labelOf(s.key)}: at most ${s.label(Math.max(0, s.max))} remains, below the minimum of ${s.label(s.min)}.`));
    }
    replace(banners, ...nodes);
  }

  // ---------------------------------------------------------------- validation

  function setErr(key: string, text: string): void {
    const err = document.getElementById(`f-${key}-err`);
    const input = document.getElementById(`f-${key}`);
    if (err) err.textContent = text;
    if (input) { if (text) input.setAttribute('aria-invalid', 'true'); else input.removeAttribute('aria-invalid'); }
  }

  function validateSlider(s: Slider): boolean {
    const raw = s.input.value.trim();
    const v = Number(raw);
    let msg = '';
    if (s.max < s.min) msg = 'Not available with the remaining capacity.';
    else if (!raw || !Number.isFinite(v)) msg = 'Enter a number.';
    else if (!Number.isInteger(v)) msg = 'Use a whole number.';
    else if (v < s.min || v > s.max) msg = `Must be between ${s.label(s.min)} and ${s.label(s.max)}.`;
    setErr(s.key, msg);
    return !msg;
  }

  function validateName(show: boolean): boolean {
    const v = name.value;
    let msg = '';
    if (!v) msg = 'Enter a name.';
    else if (nameRe && !nameRe.test(v)) msg = 'Use 2–63 characters: lowercase letters, digits and hyphens; start with a letter and end with a letter or digit.';
    setErr('name', show ? msg : '');
    return !msg;
  }

  function validateAll(showAll = false): boolean {
    let ok = validateName(showAll || touched.has('name'));
    for (const s of Object.values(sliders)) ok = validateSlider(s) && ok;
    for (const [sel, key] of [[owner, 'owner'], [image, 'image'], [pool, 'pool'], [network, 'network']] as const) {
      const good = Boolean(sel.value);
      setErr(key, good ? '' : 'Required.');
      ok = good && ok;
    }
    const c = Number(sliders.cores.input.value), a = Number(sliders.allow.input.value);
    byId('cpu-equiv').textContent = Number.isFinite(c * a) && c > 0 && a > 0
      ? `CPU cap: ${c} × ${a} % = ${(c * a / 100).toFixed(2).replace(/\.?0+$/, '')} CPU worth of time (${c * a} ms per 100 ms).`
      : '';
    submit.disabled = submitting || !opts || Boolean(opts.bounds.blocked_reason);
    return ok && !opts?.bounds.blocked_reason;
  }

  // ---------------------------------------------------------------- events

  for (const s of Object.values(sliders)) {
    s.range.addEventListener('input', () => { s.input.value = s.range.value; validateAll(); });
    s.input.addEventListener('input', () => { s.range.value = s.input.value; validateAll(); });
  }
  name.addEventListener('input', () => { touched.add('name'); validateAll(); });
  name.addEventListener('blur', () => { touched.add('name'); validateAll(); });
  owner.addEventListener('change', () => { void loadOptions(owner.value); });
  pool.addEventListener('change', () => { if (opts) { applyBounds(opts.bounds); renderBanners(); validateAll(); } });
  for (const el of [image, network, desc, autostart, ephemeral]) el.addEventListener('change', () => validateAll());

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!opts) return;
    if (!validateAll(true)) {
      replace(submitMsg, banner('warn', 'Please fix the highlighted fields.'));
      form.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
      return;
    }
    replace(submitMsg);
    const body = {
      name: name.value,
      image: image.value,
      pool: pool.value,
      network: network.value,
      cpu_cores: sliders.cores.toApi(Number(sliders.cores.input.value)),
      cpu_allowance_pct: sliders.allow.toApi(Number(sliders.allow.input.value)),
      memory_bytes: sliders.mem.toApi(Number(sliders.mem.input.value)),
      disk_bytes: sliders.disk.toApi(Number(sliders.disk.input.value)),
      owner_id: owner.value,
      ephemeral: ephemeral.checked,
      autostart: autostart.checked,
      description: desc.value,
      options_version: opts.options_version,
    };
    const ownerEmail = opts.owners.find((o) => o.id === body.owner_id)?.email ?? body.owner_id;
    const okd = await confirmDialog({
      title: `Create ${body.name}?`,
      body: [
        kv([
          ['Owner', ownerEmail],
          ['Image', image.selectedOptions[0]?.textContent ?? body.image],
          ['Pool / network', `${body.pool} / ${body.network}`],
          ['CPU', `${cores(body.cpu_cores)} at ${pct(body.cpu_allowance_pct, 0)}`],
          ['Memory', bytes(body.memory_bytes)],
          ['Disk', bytes(body.disk_bytes)],
          ['Autostart', yesNo(body.autostart)],
          ['Ephemeral', body.ephemeral ? 'yes: deleted when stopped' : 'no'],
          ['Description', body.description || 'none'],
        ]),
        h('p', { class: 'small muted', text: `This reserves the resources against ${ownerEmail}'s quota until the container is deleted.` }),
      ],
      confirmLabel: 'Create container',
    });
    if (!okd) return;

    submitting = true;
    setBusy(submit, true, 'Creating…');
    try {
      const res = await api.post<{ operation: Operation; container_id: string }>('/api/containers', body, { idempotencyKey: submission.keyFor(body) });
      submission.settle();
      replace(submitMsg, banner('ok', 'Creation accepted.', `Operation ${res.operation.id}. Opening the container…`));
      location.assign(`/containers/view/${qs({ id: res.container_id, op: res.operation.id })}`);
    } catch (err) {
      submission.settle(err);
      submitting = false;
      setBusy(submit, false);
      replace(submitMsg, errorBlock(err, 'Container not created'),
        err instanceof ApiError && err.isRetryable ? h('p', { class: 'small', text: 'You can submit again: an identical request reuses the same request id and will not create a second container.' }) : null);
      if (err instanceof ApiError) {
        for (const [k, v] of Object.entries(err.fields)) setErr(FIELD_TO_KEY[k] ?? k, v);
        const fresh = err.extra['bounds'] as CreationBounds | undefined;
        if (fresh && opts) {
          opts = { ...opts, bounds: fresh };
          applyBounds(fresh);
          renderBanners();
          for (const s of Object.values(sliders)) validateSlider(s);
          for (const [k, v] of Object.entries(err.fields)) setErr(FIELD_TO_KEY[k] ?? k, v);
        } else if (err.isConflict && ('options_version' in err.fields || /OPTIONS|STALE/i.test(err.code))) {
          void loadOptions(owner.value);
        }
      }
      submitMsg.querySelector<HTMLElement>('[role="alert"]')?.focus();
    }
  });

  await loadOptions(ctx.me.user.id);
}

function labelOf(k: Key): string {
  return { cores: 'CPU cores', allow: 'CPU allowance', mem: 'Memory', disk: 'Disk' }[k];
}

void main().catch((err: unknown) => showLoadError(err, 'container creation'));
