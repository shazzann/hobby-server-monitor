// Formatting helpers. Every formatter maps `null`/`undefined` to DASH; use dom.value() to render
// DASH with an accessible "unknown" label.

export const DASH = '—';
export const UNKNOWN_LABEL = 'unknown';

type N = number | null | undefined;

const isNum = (v: N): v is number => typeof v === 'number' && Number.isFinite(v);

const IEC = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

export const MiB = 1024 * 1024;
export const GiB = 1024 * MiB;

/** IEC bytes: 1536 -> "1.5 KiB". */
export function bytes(v: N, digits = 1): string {
  if (!isNum(v)) return DASH;
  const neg = v < 0;
  let n = Math.abs(v);
  let i = 0;
  while (n >= 1024 && i < IEC.length - 1) { n /= 1024; i++; }
  const d = i === 0 ? 0 : n >= 100 ? 0 : digits;
  return `${neg ? '-' : ''}${n.toFixed(d).replace(/\.0+$/, '')} ${IEC[i]}`;
}

export function rate(v: N): string {
  return isNum(v) ? `${bytes(v)}/s` : DASH;
}

export function pct(v: N, digits = 1): string {
  if (!isNum(v)) return DASH;
  return `${v.toFixed(v >= 100 ? 0 : digits).replace(/\.0+$/, '')} %`;
}

/** Fraction 0..1 -> "98 %". */
export function fraction(v: N): string {
  return isNum(v) ? pct(v * 100, v * 100 < 10 ? 1 : 0) : DASH;
}

export function num(v: N, digits = 0): string {
  if (!isNum(v)) return DASH;
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

export function cores(v: N): string {
  if (!isNum(v)) return DASH;
  return `${num(v, 2)} ${v === 1 ? 'core' : 'cores'}`;
}

/** Seconds -> "3 d 4 h", "5 h 12 min", "42 s". */
export function duration(sec: N): string {
  if (!isNum(sec)) return DASH;
  const s = Math.max(0, Math.round(sec));
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  if (d > 0) return `${d} d ${h} h`;
  if (h > 0) return `${h} h ${m} min`;
  if (m > 0) return `${m} min ${r} s`;
  return `${r} s`;
}

export function ms(v: N): string {
  if (!isNum(v)) return DASH;
  return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(v < 10_000 ? 2 : 1)} s`;
}

/** Age in seconds -> "4 s ago". */
export function age(sec: N): string {
  if (!isNum(sec)) return DASH;
  if (sec < 1) return 'just now';
  return `${duration(sec)} ago`;
}

export function parseTime(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Relative age from an ISO timestamp. */
export function since(iso: string | null | undefined): string {
  const d = parseTime(iso);
  return d ? age((Date.now() - d.getTime()) / 1000) : DASH;
}

export function dateTime(iso: string | null | undefined): string {
  const d = parseTime(iso);
  return d ? d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'medium' }) : DASH;
}

export function epochTime(sec: number, withDate: boolean): string {
  const d = new Date(sec * 1000);
  return withDate
    ? d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

export function yesNo(v: boolean | null | undefined): string {
  return v === true ? 'yes' : v === false ? 'no' : DASH;
}

/** "used / limit" with either side possibly unknown. */
export function usedOf(used: N, limit: N, f: (v: N) => string = bytes): string {
  if (!isNum(used) && !isNum(limit)) return DASH;
  return `${isNum(used) ? f(used) : UNKNOWN_LABEL} / ${isNum(limit) ? f(limit) : UNKNOWN_LABEL}`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
