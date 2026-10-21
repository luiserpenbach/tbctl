// Live sample buffers, kept OUTSIDE React state: WS frames arrive at 25 Hz
// with arrays; charts read imperatively (uPlot setData) on a tick signal.

import uPlot from "uplot";
import type { FrameMsg } from "./types";

const MAX_AGE_S = 330; // keep a little over 5 min for backfill windows

class DataBuffers {
  private t: Map<string, number[]> = new Map();
  private v: Map<string, number[]> = new Map();

  appendFrame(f: FrameMsg) {
    f.tags.forEach((tag, i) => {
      const col = f.data[i];
      let ts = this.t.get(tag);
      let vs = this.v.get(tag);
      if (!ts || !vs) {
        ts = [];
        vs = [];
        this.t.set(tag, ts);
        this.v.set(tag, vs);
      }
      // drop any overlap (backfill may already have covered these times)
      const lastT = ts.length ? ts[ts.length - 1] : -Infinity;
      for (let k = 0; k < col.length; k++) {
        const tk = f.t[k];
        if (tk > lastT) {
          ts.push(tk);
          vs.push(col[k]);
        }
      }
      this.trim(tag);
    });
    markDirty();
  }

  seed(tag: string, t: number[], v: number[]) {
    const ts = this.t.get(tag) ?? [];
    if (ts.length === 0 || (t.length && t[t.length - 1] < ts[0])) {
      // prepend backfill before anything already streamed
      const curT = this.t.get(tag) ?? [];
      const curV = this.v.get(tag) ?? [];
      this.t.set(tag, [...t, ...curT]);
      this.v.set(tag, [...v, ...curV]);
      this.trim(tag);
    }
  }

  private trim(tag: string) {
    const ts = this.t.get(tag)!;
    const vs = this.v.get(tag)!;
    const cutoff = ts[ts.length - 1] - MAX_AGE_S;
    let drop = 0;
    while (drop < ts.length && ts[drop] < cutoff) drop++;
    if (drop > 0) {
      ts.splice(0, drop);
      vs.splice(0, drop);
    }
  }

  latest(tag: string): number | null {
    const vs = this.v.get(tag);
    return vs && vs.length ? vs[vs.length - 1] : null;
  }

  // stale-data guard (phase 15): a dead sensor must not look like a frozen
  // live value. Compared against the freshest tag rather than wall clock so
  // browser/node clock skew can't grey out everything.
  isStale(tag: string, maxAgeS = 1.0): boolean {
    const ts = this.t.get(tag);
    if (!ts || ts.length === 0) return true;
    const newest = this.latestTime();
    if (newest === null) return true;
    return newest - ts[ts.length - 1] > maxAgeS;
  }

  latestTime(): number | null {
    let best: number | null = null;
    this.t.forEach((ts) => {
      if (ts.length) best = Math.max(best ?? -Infinity, ts[ts.length - 1]);
    });
    return best;
  }

  /**
   * uPlot-aligned data for a set of tags (null-joined where timebases differ).
   * `since` limits it to t >= since and `maxPoints` thins it to a min/max
   * envelope of that many buckets: what a live chart needs, at a cost set by
   * its width rather than by the buffer (1 kHz x 5 min = 330k samples/tag).
   */
  aligned(tags: string[], opts: { since?: number; maxPoints?: number } = {}): uPlot.AlignedData {
    const present = tags.filter((tag) => this.t.get(tag)?.length);
    if (present.length === 0) return [[], ...tags.map(() => [])] as unknown as uPlot.AlignedData;
    const cols = present.map((tag) => {
      const ts = this.t.get(tag)!;
      return { ts, vs: this.v.get(tag)!, i0: opts.since === undefined ? 0 : lowerBound(ts, opts.since) };
    });
    const { ts: x0, i0: s0 } = cols[0];
    const n = x0.length - s0;
    const wanted = opts.maxPoints && n > 2 * opts.maxPoints ? Math.floor(opts.maxPoints) : 0;
    // one rate group = one timebase: share x, nothing to join
    const sameBase = cols.every(
      ({ ts, i0 }) => ts.length - i0 === n && ts[i0] === x0[s0] && ts[ts.length - 1] === x0[x0.length - 1],
    );
    let joined: (number | null | undefined)[][];
    if (sameBase && wanted) {
      // thin straight from the buffers: no window copy
      joined = envelope(x0, cols.map((c) => c.vs), s0, x0.length, wanted);
    } else {
      // copies: uPlot keeps what it is given, and frames mutate the buffers
      const sliced = cols.map(({ ts, vs, i0 }) => [ts.slice(i0), vs.slice(i0)]);
      joined = (
        sameBase ? [sliced[0][0], ...sliced.map(([, vs]) => vs)] : uPlot.join(sliced as unknown as uPlot.AlignedData[])
      ) as (number | null | undefined)[][];
      if (opts.maxPoints && joined[0].length > 2 * opts.maxPoints) {
        joined = envelope(joined[0] as number[], joined.slice(1), 0, joined[0].length, Math.floor(opts.maxPoints));
      }
    }
    // re-insert empty series for absent tags to keep series order stable
    const out: (number | null | undefined)[][] = [joined[0]];
    let j = 1;
    for (const tag of tags) {
      out.push(present.includes(tag) ? joined[j++] : []);
    }
    return out as uPlot.AlignedData;
  }
}

/** First index with ts[i] >= t (ts ascending). */
function lowerBound(ts: number[], t: number): number {
  let lo = 0;
  let hi = ts.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (ts[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/**
 * Min/max envelope of xs[start..end) over `buckets` equal index ranges: two
 * points per bucket (at its first and last time) carrying each series' min
 * and max, so peaks survive at any zoom. Gaps (null / NaN) are skipped; an
 * all-gap bucket stays a gap.
 */
function envelope(
  xs: number[],
  ys: (number | null | undefined)[][],
  start: number,
  end: number,
  buckets: number,
): (number | null)[][] {
  const n = end - start;
  const out: (number | null)[][] = [[], ...ys.map(() => [])];
  for (let b = 0; b < buckets; b++) {
    const i0 = start + Math.floor((b * n) / buckets);
    const i1 = start + Math.floor(((b + 1) * n) / buckets);
    if (i1 <= i0) continue;
    out[0].push(xs[i0], xs[i1 - 1]);
    for (let s = 0; s < ys.length; s++) {
      const y = ys[s];
      let lo = Infinity;
      let hi = -Infinity;
      for (let i = i0; i < i1; i++) {
        const v = y[i];
        if (v == null || v !== v) continue; // null or NaN
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (lo === Infinity) out[s + 1].push(null, null);
      else out[s + 1].push(lo, hi);
    }
  }
  return out;
}

export const buffers = new DataBuffers();

// Chart refresh signal: listeners run soon after new data arrived (frames
// land at 25 Hz), but adaptively throttled so drawing charts takes at most
// ~25 % of the main thread: a fast machine redraws at 25 Hz, a slow or
// software-rendered one backs off instead of freezing the UI. Not done in
// requestAnimationFrame: under software rendering that forces a synchronous
// canvas flush per frame (measured ~5x the cost).
export type TickListener = () => void;
const listeners = new Set<TickListener>();
const MIN_INTERVAL_MS = 40; // = frame rate from the node
const MAX_INTERVAL_MS = 250;
const BUDGET = 0.25; // share of the main thread charts may use
let dirty = false;
let scheduled = false;
let running = false;
let lastRun = 0;
let avgCostMs = 0;
let intervalMs = MIN_INTERVAL_MS;
export function onTick(fn: TickListener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
function schedule() {
  if (scheduled) return;
  scheduled = true;
  setTimeout(runTick, Math.max(0, lastRun + intervalMs - performance.now()));
}
function runTick() {
  scheduled = false;
  if (!dirty || running) return;
  dirty = false;
  running = true;
  const queue = [...listeners];
  const t0 = performance.now();
  let cost = 0;
  // One chart per task, so clicks and keys get in between charts. Each
  // chart's cost is timed up to the next task, which includes uPlot's own
  // deferred draw (it runs after the listener returns).
  const step = (prevStart: number) => {
    if (prevStart) cost += performance.now() - prevStart;
    const fn = queue.shift();
    if (fn) {
      const start = performance.now();
      try {
        fn();
      } catch (e) {
        console.error("chart tick failed", e); // one bad chart must not stop the rest
      }
      setTimeout(() => step(start), 0);
      return;
    }
    running = false;
    lastRun = t0;
    avgCostMs = 0.8 * avgCostMs + 0.2 * cost;
    intervalMs = Math.min(MAX_INTERVAL_MS, Math.max(MIN_INTERVAL_MS, avgCostMs / BUDGET));
    if (dirty) schedule();
  };
  step(0);
}
export function markDirty() {
  dirty = true;
  if (!running) schedule();
}

// Text readouts (sidebar, value tables, P&ID labels) refresh at 10 Hz:
// faster numbers are unreadable and only cost renders.
const slowListeners = new Set<TickListener>();
export function onSlowTick(fn: TickListener): () => void {
  slowListeners.add(fn);
  return () => slowListeners.delete(fn);
}
setInterval(() => slowListeners.forEach((fn) => fn()), 100);
