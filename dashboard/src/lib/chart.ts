// Tiny dependency-free SVG line chart.
// - the line breaks on `null` points and on missing intervals (never connects across gaps)
// - min/max axis labels plus start/end time labels
// - accessible: role="img" with <title>/<desc>, and a visible text summary (min/avg/max, coverage)
// - at most 600 points per series (bucket-averaged when longer)

import { h, svg } from './dom';
import { epochTime, fraction } from './format';
import type { Point } from './types';

export interface ChartSeries {
  name: string;
  points: Point[];
}

export interface ChartOptions {
  title: string;
  series: ChartSeries[];
  start: number;
  end: number;
  resolution: number;
  format: (v: number | null) => string;
  /** Overall coverage 0..1 reported by the API (falls back to non-null share). */
  coverage?: number | null;
  gaps?: [number, number][];
}

const MAX_POINTS = 600;
const W = 600, H = 160, PAD_L = 0, PAD_R = 0, PAD_T = 6, PAD_B = 6;

function downsample(points: Point[]): Point[] {
  if (points.length <= MAX_POINTS) return points;
  const size = Math.ceil(points.length / MAX_POINTS);
  const out: Point[] = [];
  for (let i = 0; i < points.length; i += size) {
    const bucket = points.slice(i, i + size);
    const vals = bucket.map((p) => p[1]).filter((v): v is number => v !== null);
    out.push([bucket[0]![0], vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null]);
  }
  return out;
}

interface Stats { min: number; max: number; avg: number; count: number; total: number }

function stats(points: Point[]): Stats | null {
  let min = Infinity, max = -Infinity, sum = 0, count = 0;
  for (const [, v] of points) {
    if (v === null || !Number.isFinite(v)) continue;
    min = Math.min(min, v); max = Math.max(max, v); sum += v; count++;
  }
  return count ? { min, max, avg: sum / count, count, total: points.length } : null;
}

let chartSeq = 0;

export function lineChart(opts: ChartOptions): HTMLElement {
  const series = opts.series.map((s) => ({ ...s, points: downsample(s.points), factor: Math.max(1, Math.ceil(s.points.length / MAX_POINTS)) }));
  const allStats = series.map((s) => stats(s.points));
  const anyData = allStats.some((s) => s !== null);
  const fig = h('figure', { class: 'chart-card' });
  fig.appendChild(h('h3', { text: opts.title }));
  if (series.length > 1) {
    fig.appendChild(h('ul', { class: 'legend' }, ...series.map((s, i) =>
      h('li', null, h('span', { class: `sw series-${i}`, attrs: { 'aria-hidden': 'true' } }), s.name))));
  }

  if (!anyData) {
    fig.appendChild(h('div', { class: 'chart-empty', attrs: { role: 'note' } }, 'No data in this period.'));
    return fig;
  }

  const dataMax = Math.max(...allStats.map((s) => (s ? s.max : 0)));
  const dataMin = Math.min(...allStats.map((s) => (s ? s.min : 0)));
  const yMin = Math.min(0, dataMin);
  const yMax = dataMax > yMin ? yMin + (dataMax - yMin) * 1.1 : yMin + 1;
  const span = Math.max(1, opts.end - opts.start);
  const x = (t: number) => PAD_L + ((t - opts.start) / span) * (W - PAD_L - PAD_R);
  const y = (v: number) => PAD_T + (1 - (v - yMin) / (yMax - yMin)) * (H - PAD_T - PAD_B);
  const withDate = span > 86400;

  const id = `chart-${++chartSeq}`;
  const summary = summaryText(opts, series, allStats);
  const root = svg('svg', { class: 'chart-svg', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-labelledby': `${id}-t ${id}-d`, preserveAspectRatio: 'none', focusable: 'false' },
    svg('title', { id: `${id}-t` }, opts.title),
    svg('desc', { id: `${id}-d` }, summary));

  // gaps reported by the API (shaded), only when wider than two intervals
  for (const [gs, ge] of opts.gaps ?? []) {
    if (ge - gs < opts.resolution * 2) continue;
    const x0 = x(Math.max(gs, opts.start)), x1 = x(Math.min(ge, opts.end));
    if (x1 - x0 < 1) continue;
    root.appendChild(svg('rect', { class: 'gap', x: x0.toFixed(1), y: PAD_T, width: (x1 - x0).toFixed(1), height: H - PAD_T - PAD_B }));
  }

  // grid + y labels
  for (const v of [yMin, (yMin + yMax) / 2, yMax]) {
    root.appendChild(svg('line', { class: 'grid-line', x1: PAD_L, x2: W - PAD_R, y1: y(v).toFixed(1), y2: y(v).toFixed(1) }));
  }

  series.forEach((s, i) => {
    // Points further apart than 2.5 intervals mean missing samples: never draw across them.
    const maxStep = Math.max(opts.resolution, 1) * 2.5 * s.factor;
    let d = '';
    let segLen = 0;
    let prevT: number | null = null;
    const singles: [number, number][] = [];
    let segStart: [number, number] | null = null;
    const endSeg = () => {
      if (segLen === 1 && segStart) singles.push(segStart);
      segLen = 0; segStart = null;
    };
    for (const [t, v] of s.points) {
      if (v === null || !Number.isFinite(v) || t < opts.start || t > opts.end) { endSeg(); prevT = null; continue; }
      if (prevT !== null && t - prevT > maxStep) endSeg();
      const px = x(t).toFixed(1), py = y(v).toFixed(1);
      if (segLen === 0) { d += `M${px} ${py}`; segStart = [x(t), y(v)]; } else d += `L${px} ${py}`;
      segLen++;
      prevT = t;
    }
    endSeg();
    if (d) root.appendChild(svg('path', { class: `line series-${i}`, d, 'vector-effect': 'non-scaling-stroke' }));
    for (const [cx, cy] of singles) root.appendChild(svg('path', { class: `line series-${i} dot`, d: `M${cx.toFixed(1)} ${cy.toFixed(1)}h0.01`, 'vector-effect': 'non-scaling-stroke' }));
  });

  // Axis labels are HTML so they stay legible at any width (the SVG stretches horizontally).
  fig.appendChild(h('div', { class: 'chart-plot' },
    h('div', { class: 'y-axis', attrs: { 'aria-hidden': 'true' } }, h('span', { text: opts.format(yMax) }), h('span', { text: opts.format(yMin) })),
    root,
    h('div', { class: 'x-axis', attrs: { 'aria-hidden': 'true' } }, h('span', { text: epochTime(opts.start, withDate) }), h('span', { text: epochTime(opts.end, withDate) }))));
  fig.appendChild(h('figcaption', { class: 'chart-summary', text: summary }));
  return fig;
}

function summaryText(opts: ChartOptions, series: ChartSeries[], allStats: (Stats | null)[]): string {
  const parts = series.map((s, i) => {
    const st = allStats[i];
    const label = series.length > 1 ? `${s.name}: ` : '';
    if (!st) return `${label}no data`;
    return `${label}min ${opts.format(st.min)}, avg ${opts.format(st.avg)}, max ${opts.format(st.max)}`;
  });
  const first = allStats.find((s) => s) ?? null;
  const cov = opts.coverage ?? (first ? first.count / Math.max(1, first.total) : null);
  return `${parts.join('; ')}. Coverage ${cov === null ? 'unknown' : fraction(cov)} of the period; gaps are shown as breaks in the line.`;
}
