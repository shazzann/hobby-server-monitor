// Visible-tab-only polling.
// - never overlapping: the next run is scheduled only after the previous one settles
// - paused while the tab is hidden; one immediate refresh when it becomes visible / focused again
// - exponential backoff on failure (interval * 2^n, capped at 60 s)
// - stops on pagehide, on stop(), or when the task returns `false` (e.g. operation reached a terminal state)

export interface PollerOptions {
  /** Milliseconds between the end of one run and the start of the next. May be a function for adaptive intervals. */
  interval: number | (() => number);
  /** Return `false` to stop polling. Throw to trigger backoff. */
  task: (signal: AbortSignal) => Promise<boolean | void>;
  maxBackoffMs?: number;
  onError?: (err: unknown, retryInMs: number) => void;
  /** Run immediately on start (default true). */
  immediate?: boolean;
}

const pollers = new Set<Poller>();
let listenersInstalled = false;

function installListeners(): void {
  if (listenersInstalled) return;
  listenersInstalled = true;
  document.addEventListener('visibilitychange', () => {
    for (const p of pollers) document.hidden ? p.pause() : p.resume();
  });
  window.addEventListener('focus', () => { for (const p of pollers) p.resume(); });
  window.addEventListener('pagehide', () => { for (const p of [...pollers]) p.stop(); });
}

export class Poller {
  private readonly opts: PollerOptions;
  private timer: number | undefined;
  private inFlight = false;
  private stopped = true;
  private failures = 0;
  private lastRunAt = 0;
  private controller: AbortController | null = null;

  constructor(opts: PollerOptions) {
    this.opts = opts;
  }

  start(): this {
    installListeners();
    this.stopped = false;
    pollers.add(this);
    if (this.opts.immediate === false) this.schedule(this.baseInterval());
    else if (!document.hidden) void this.run();
    return this;
  }

  stop(): void {
    this.stopped = true;
    pollers.delete(this);
    this.clearTimer();
    this.controller?.abort();
    this.controller = null;
  }

  get running(): boolean { return !this.stopped; }

  /** Run now unless a request is already in flight (manual "Refresh"). */
  refreshNow(): void {
    if (this.stopped || this.inFlight) return;
    this.clearTimer();
    void this.run();
  }

  /** Called when the tab is hidden: no new requests are started. */
  pause(): void {
    this.clearTimer();
  }

  /** Called on visible/focus: one immediate refresh (debounced to avoid visibilitychange+focus double runs). */
  resume(): void {
    if (this.stopped || this.inFlight || document.hidden) return;
    if (Date.now() - this.lastRunAt < 1000) {
      if (this.timer === undefined) this.schedule(this.baseInterval());
      return;
    }
    this.clearTimer();
    void this.run();
  }

  private baseInterval(): number {
    const i = this.opts.interval;
    return typeof i === 'function' ? i() : i;
  }

  private clearTimer(): void {
    if (this.timer !== undefined) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
  }

  private schedule(ms: number): void {
    this.clearTimer();
    if (this.stopped || document.hidden) return;
    this.timer = window.setTimeout(() => {
      this.timer = undefined;
      void this.run();
    }, ms);
  }

  private async run(): Promise<void> {
    if (this.stopped || this.inFlight) return;
    this.inFlight = true;
    this.lastRunAt = Date.now();
    this.controller = new AbortController();
    let next = this.baseInterval();
    try {
      const result = await this.opts.task(this.controller.signal);
      this.failures = 0;
      if (result === false) {
        this.stop();
        return;
      }
    } catch (err) {
      if (this.stopped) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      this.failures += 1;
      const max = this.opts.maxBackoffMs ?? 60_000;
      next = Math.min(max, Math.max(this.baseInterval(), 1000) * 2 ** this.failures);
      this.opts.onError?.(err, next);
    } finally {
      this.inFlight = false;
      this.controller = null;
    }
    this.schedule(next);
  }
}
