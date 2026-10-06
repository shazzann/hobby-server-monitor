// Same-origin fetch wrapper for the HSM API.
// - credentials: 'same-origin'; session cookie is HttpOnly and never touched here.
// - caches GET /api/me (incl. csrf_token) in memory only; nothing is written to storage.
// - adds X-CSRF-Token on unsafe methods; optional Idempotency-Key.
// - parses the error envelope into ApiError. 401 -> /login/. 403 -> thrown for the page to render "denied".

import type { Me } from './types';

const UNSAFE = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: Record<string, string>;
  readonly requestId: string | null;
  /** Extra members of the error object (e.g. `bounds` on 409 create). */
  readonly extra: Record<string, unknown>;

  constructor(status: number, code: string, message: string, fields: Record<string, string> = {}, requestId: string | null = null, extra: Record<string, unknown> = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.fields = fields;
    this.requestId = requestId;
    this.extra = extra;
  }

  get isDenied(): boolean { return this.status === 403; }
  get isNotFound(): boolean { return this.status === 404; }
  get isConflict(): boolean { return this.status === 409; }
  /** True when the request may not have reached the server or the server could not decide: safe to retry with the same idempotency key. */
  get isRetryable(): boolean { return this.status === 0 || this.status === 429 || this.status >= 500; }
}

export interface RequestOptions {
  body?: unknown;
  idempotencyKey?: string;
  signal?: AbortSignal;
  /** Skip the CSRF header (public pre-login endpoints such as /auth/admission-context). */
  csrf?: boolean;
  /** Do not redirect to /login/ on 401; throw instead (login page probing). */
  noAuthRedirect?: boolean;
}

let mePromise: Promise<Me> | null = null;
let redirecting = false;

export function clearSession(): void {
  mePromise = null;
}

export function getMe(force = false, signal?: AbortSignal): Promise<Me> {
  if (force || !mePromise) {
    const opts: RequestOptions = {};
    if (signal) opts.signal = signal;
    mePromise = request<Me>('GET', '/api/me', opts);
    mePromise.catch(() => { mePromise = null; });
  }
  return mePromise;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

async function toApiError(res: Response): Promise<ApiError> {
  let payload: unknown = null;
  try { payload = await res.json(); } catch { /* not JSON */ }
  const err = asRecord(asRecord(payload)?.['error']);
  if (err) {
    const { code, message, fields, request_id, ...extra } = err;
    const f: Record<string, string> = {};
    const fr = asRecord(fields);
    if (fr) for (const [k, v] of Object.entries(fr)) f[k] = String(v);
    return new ApiError(res.status, typeof code === 'string' ? code : `HTTP_${res.status}`,
      typeof message === 'string' ? message : res.statusText || 'Request failed', f,
      typeof request_id === 'string' ? request_id : null, extra);
  }
  return new ApiError(res.status, `HTTP_${res.status}`, defaultMessage(res.status));
}

function defaultMessage(status: number): string {
  switch (status) {
    case 400: return 'The request was malformed.';
    case 401: return 'Your session has ended. Please sign in again.';
    case 403: return 'You do not have access to this.';
    case 404: return 'Not found.';
    case 409: return 'The request conflicts with the current state.';
    case 422: return 'Some values are invalid.';
    case 429: return 'Too many requests. Please wait a moment and try again.';
    case 503: return 'A required service is temporarily unavailable.';
    default: return status >= 500 ? 'The server had a problem handling this request.' : 'Request failed.';
  }
}

function redirectToLogin(): void {
  if (redirecting) return;
  redirecting = true;
  clearSession();
  location.assign('/login/');
}

async function request<T>(method: string, path: string, opts: RequestOptions = {}, retriedCsrf = false): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  if (UNSAFE.has(method) && opts.csrf !== false) {
    const me = await getMe();
    headers['X-CSRF-Token'] = me.csrf_token;
  }
  if (opts.idempotencyKey) headers['Idempotency-Key'] = opts.idempotencyKey;

  const init: RequestInit = { method, headers, credentials: 'same-origin', cache: 'no-store' };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  if (opts.signal) init.signal = opts.signal;

  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e;
    throw new ApiError(0, 'NETWORK', 'Could not reach the server. Check your connection.');
  }

  if (res.status === 401) {
    const err = await toApiError(res);
    if (!opts.noAuthRedirect) redirectToLogin();
    throw err;
  }
  if (!res.ok) {
    const err = await toApiError(res);
    // A rotated/expired CSRF token: refresh /api/me once and retry the same request (same idempotency key).
    if (err.status === 403 && /CSRF/i.test(err.code) && UNSAFE.has(method) && opts.csrf !== false && !retriedCsrf) {
      await getMe(true);
      return request<T>(method, path, opts, true);
    }
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

export const api = {
  get: <T>(path: string, opts?: RequestOptions) => request<T>('GET', path, opts),
  post: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('POST', path, { ...opts, body }),
  put: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('PUT', path, { ...opts, body }),
  patch: <T>(path: string, body?: unknown, opts: RequestOptions = {}) => request<T>('PATCH', path, { ...opts, body }),
  del: <T>(path: string, opts: RequestOptions = {}) => request<T>('DELETE', path, opts),
};

/** Builds a query string with encoded values (ids, names). */
export function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '') sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : '';
}

export const enc = encodeURIComponent;

/**
 * One Idempotency-Key per user-intended submission.
 * `keyFor(body)` reuses the current key while the body is unchanged and the previous attempt
 * ended without a definitive answer (network error, 429, 5xx). Call `settle()` after a definitive
 * response (2xx or a 4xx decision) so the next click is a new submission with a fresh key.
 */
export class Submission {
  private key: string | null = null;
  private bodyJson: string | null = null;

  keyFor(body: unknown): string {
    const json = JSON.stringify(body ?? null);
    if (!this.key || json !== this.bodyJson) {
      this.key = crypto.randomUUID();
      this.bodyJson = json;
    }
    return this.key;
  }

  /** Call with the outcome of the attempt: keeps the key only when a retry of the same submission makes sense. */
  settle(err?: unknown): void {
    if (err instanceof ApiError && err.isRetryable) return;
    if (err instanceof DOMException && err.name === 'AbortError') return;
    this.key = null;
    this.bodyJson = null;
  }
}

export async function logout(): Promise<void> {
  try {
    await request<void>('POST', '/auth/logout', { noAuthRedirect: true });
  } catch {
    // Even if the server call fails, drop local state and leave.
  }
  clearSession();
  location.replace('/login/');
}
