import { api, enc, qs } from '../../lib/api';
import { lineChart } from '../../lib/chart';
import { byId, h, replace, stat } from '../../lib/dom';
import { DASH, bytes, dateTime, duration, fraction, num, pct, rate } from '../../lib/format';
import { Poller } from '../../lib/poll';
import { describeError } from '../../lib/shell';
import type { History, MetricName, Range, Usage } from '../../lib/types';
import type { ViewCtx } from './context';

const METRICS: MetricName[] = ['cpu_pct', 'memory_bytes', 'disk_bytes', 'rx_rate', 'tx_rate'];
const RANGE_SECONDS: Record<Range, number> = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000 };

function selectedRange(): Range {
  const el = document.querySelector<HTMLInputElement>('input[name="range"]:checked');
  const v = el?.value as Range | undefined;
  return v && v in RANGE_SECONDS ? v : '1h';
}

export function setupHistory(ctx: ViewCtx): void {
  let rendered: Range | null = null;

  const poller = new Poller({
    // Short ranges change quickly; long ranges are rollups that change slowly.
    interval: () => (RANGE_SECONDS[selectedRange()] <= 21600 ? 30_000 : 300_000),
    task: async (signal) => {
      // Loop so that a range change during an in-flight request is not lost.
      for (;;) {
        const range = selectedRange();
        if (rendered !== range) byId('hist-meta').textContent = 'Loading…';
        const [hist, usage] = await Promise.all([
          api.get<History>(`/api/containers/${enc(ctx.id)}/history${qs({ range, metrics: METRICS.join(','), max_points: 300 })}`, { signal }),
          api.get<Usage>(`/api/containers/${enc(ctx.id)}/usage${qs({ range })}`, { signal }),
        ]);
        if (range !== selectedRange()) continue;
        renderHistory(hist, range);
        renderUsage(usage, range);
        rendered = range;
        return;
      }
    },
    onError: (err, retryMs) => {
      byId('hist-meta').textContent = `Could not load history: ${describeError(err)} Retrying in ${Math.round(retryMs / 1000)} s.`;
    },
  }).start();

  byId('range-picker').addEventListener('change', () => poller.refreshNow());
  byId('hist-refresh').addEventListener('click', () => poller.refreshNow());
}

function renderHistory(hist: History, range: Range): void {
  const s = hist.series;
  const p = (m: MetricName) => s[m]?.points ?? [];
  const base = { start: hist.start, end: hist.end, resolution: hist.resolution_seconds, coverage: hist.coverage, gaps: hist.gaps };
  const charts = [
    lineChart({ ...base, title: 'CPU (% of allocated cores)', series: [{ name: 'CPU', points: p('cpu_pct') }], format: (v) => pct(v) }),
    lineChart({ ...base, title: 'Memory used', series: [{ name: 'Memory', points: p('memory_bytes') }], format: (v) => bytes(v) }),
    lineChart({ ...base, title: 'Disk used', series: [{ name: 'Disk', points: p('disk_bytes') }], format: (v) => bytes(v) }),
    lineChart({ ...base, title: 'Network rate', series: [{ name: 'RX (received)', points: p('rx_rate') }, { name: 'TX (sent)', points: p('tx_rate') }], format: (v) => rate(v) }),
  ];
  replace(byId('charts'), ...charts);

  const parts = [
    `Range ${range}`,
    `resolution ${duration(hist.resolution_seconds)} (${hist.source === 'raw' ? 'raw samples' : '5-minute rollups'})`,
    `coverage ${hist.coverage === null ? 'unknown' : fraction(hist.coverage)}`,
  ];
  if (hist.available_from !== null && hist.available_from > hist.start) {
    parts.push(`history available from ${dateTime(new Date(hist.available_from * 1000).toISOString())}`);
  }
  byId('hist-meta').textContent = `${parts.join(' · ')}.`;
}

function renderUsage(u: Usage, range: Range): void {
  byId('usage-title').textContent = `Usage in the last ${range}`;
  const box = byId('usage');
  if (!u.coverage || !u.covered_seconds) {
    replace(box, h('div', { class: 'state-box' }, 'No measured usage in this period.'));
    return;
  }
  replace(box,
    h('div', { class: 'stat-grid' },
      stat('CPU', u.cpu_core_hours === null ? DASH : `${num(u.cpu_core_hours, 3)} core-hours`),
      stat('Memory', u.memory_gib_hours === null ? DASH : `${num(u.memory_gib_hours, 3)} GiB-hours`),
      stat('Disk average', bytes(u.disk_avg_bytes), `max ${bytes(u.disk_max_bytes)}`),
      stat('Received', bytes(u.rx_bytes)),
      stat('Sent', bytes(u.tx_bytes)),
      stat('Coverage', fraction(u.coverage), `${duration(u.covered_seconds)} measured`)));
}
