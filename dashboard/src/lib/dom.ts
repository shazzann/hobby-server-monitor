// Minimal DOM builder. All text goes through text nodes / textContent; never innerHTML.
// Inline `style` attributes are refused (CSP style-src 'self'); set CSSOM properties instead.

import { DASH, UNKNOWN_LABEL } from './format';

export type Child = Node | string | number | null | undefined | false;
type AttrValue = string | number | boolean | null | undefined;

export interface Props {
  class?: string;
  text?: string;
  attrs?: Record<string, AttrValue>;
  on?: Partial<{ [K in keyof HTMLElementEventMap]: (ev: HTMLElementEventMap[K]) => void }>;
}

function setAttrs(node: Element, attrs: Record<string, AttrValue>): void {
  for (const [k, v] of Object.entries(attrs)) {
    const key = k.toLowerCase();
    if (key === 'style' || key.startsWith('on')) throw new Error(`refusing attribute ${k}`);
    if (v === false || v === null || v === undefined) continue;
    node.setAttribute(k, v === true ? '' : String(v));
  }
}

export function append(parent: Node, ...children: Child[]): void {
  for (const c of children) {
    if (c === null || c === undefined || c === false) continue;
    parent.appendChild(typeof c === 'string' || typeof c === 'number' ? document.createTextNode(String(c)) : c);
  }
}

export function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Props | null = null, ...children: Child[]): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (props) {
    if (props.class) node.className = props.class;
    if (props.attrs) setAttrs(node, props.attrs);
    if (props.text !== undefined) node.textContent = props.text;
    if (props.on) {
      for (const [ev, fn] of Object.entries(props.on)) node.addEventListener(ev, fn as EventListener);
    }
  }
  append(node, ...children);
  return node;
}

const SVG_NS = 'http://www.w3.org/2000/svg';

export function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, AttrValue> = {}, ...children: Child[]): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  setAttrs(node, attrs);
  append(node, ...children);
  return node;
}

export function clear(node: Element): void {
  node.replaceChildren();
}

export function replace(node: Element, ...children: Child[]): void {
  node.replaceChildren();
  append(node, ...children);
}

export function byId<T extends HTMLElement = HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el as T;
}

/** A formatted value; DASH renders with an accessible "unknown" label. */
export function value(text: string, cls = ''): HTMLSpanElement {
  if (text === DASH) {
    return h('span', { class: `unknown ${cls}`.trim(), attrs: { title: UNKNOWN_LABEL } },
      h('span', { attrs: { 'aria-hidden': 'true' }, text: DASH }),
      h('span', { class: 'sr-only', text: UNKNOWN_LABEL }));
  }
  return h('span', cls ? { class: cls, text } : { text });
}

export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'neutral';

/** Status is always text + colour. */
export function badge(text: string, tone: Tone = 'neutral', title?: string): HTMLSpanElement {
  return h('span', { class: `badge ${tone}`, text, attrs: { title } });
}

export function banner(tone: 'info' | 'warn' | 'bad' | 'ok', title: string, ...body: Child[]): HTMLDivElement {
  return h('div', { class: `banner ${tone}`, attrs: { role: tone === 'bad' ? 'alert' : 'status' } },
    h('p', null, h('span', { class: 'banner-title', text: title }), body.length ? ' ' : null, ...body));
}

export function kv(rows: [string, Child | Child[]][]): HTMLDListElement {
  const dl = h('dl', { class: 'kv' });
  for (const [k, v] of rows) {
    const dd = h('dd');
    if (Array.isArray(v)) append(dd, ...v); else append(dd, typeof v === 'string' ? value(v) : v);
    append(dl, h('dt', { text: k }), dd);
  }
  return dl;
}

export function stat(label: string, val: string, sub?: Child): HTMLDivElement {
  return h('div', { class: 'stat' },
    h('div', { class: 'label', text: label }),
    h('div', { class: 'value' }, value(val)),
    sub ? h('div', { class: 'sub' }, sub) : null);
}

/** Table cell with a data-label for the stacked narrow layout. */
export function td(label: string, content: Child | Child[], cls = ''): HTMLTableCellElement {
  const cell = h('td', { attrs: { 'data-label': label, class: cls || undefined } });
  if (Array.isArray(content)) append(cell, ...content); else append(cell, typeof content === 'string' ? value(content) : content);
  return cell;
}

export function setBusy(button: HTMLButtonElement, busy: boolean, busyText?: string): void {
  if (busy) {
    if (!button.dataset['label']) button.dataset['label'] = button.textContent ?? '';
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    if (busyText) button.textContent = busyText;
  } else {
    button.disabled = false;
    button.removeAttribute('aria-busy');
    if (button.dataset['label'] !== undefined) button.textContent = button.dataset['label'];
  }
}

/** Opens a <dialog>; resolves true when confirmed (form submitted with value "confirm"). */
export function confirmDialog(opts: {
  title: string;
  body: Child[];
  confirmLabel: string;
  danger?: boolean;
  /** Extra controls; `isValid` decides whether confirm is enabled. */
  controls?: HTMLElement[];
  isValid?: () => boolean;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const titleId = `dlg-${crypto.randomUUID()}`;
    const confirmBtn = h('button', { class: opts.danger ? 'danger solid' : 'primary', text: opts.confirmLabel, attrs: { type: 'submit', value: 'confirm' } });
    // type=button so Enter in a text field never submits as Cancel; the default button is Confirm.
    const cancelBtn = h('button', { text: 'Cancel', attrs: { type: 'button' } });
    const form = h('form', { attrs: { method: 'dialog' } },
      h('h2', { text: opts.title, attrs: { id: titleId } }),
      ...opts.body,
      ...(opts.controls ?? []),
      h('div', { class: 'actions' }, cancelBtn, confirmBtn));
    const dlg = h('dialog', { attrs: { 'aria-labelledby': titleId } }, form);
    cancelBtn.addEventListener('click', () => dlg.close('cancel'));
    const sync = () => { confirmBtn.disabled = opts.isValid ? !opts.isValid() : false; };
    form.addEventListener('input', sync);
    form.addEventListener('change', sync);
    sync();
    dlg.addEventListener('close', () => {
      resolve(dlg.returnValue === 'confirm');
      dlg.remove();
    });
    document.body.appendChild(dlg);
    dlg.showModal();
    (opts.controls?.length ? (form.querySelector('input,select,textarea') as HTMLElement | null) : cancelBtn)?.focus();
  });
}

export function fieldId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 9)}`;
}
