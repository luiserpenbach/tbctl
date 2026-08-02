// Live sample buffers, kept OUTSIDE React state: WS frames arrive at ~10 Hz
// with arrays; charts read imperatively (uPlot setData) on a tick signal.

import uPlot from "uplot";
import type { FrameMsg } from "./types";

const MAX_AGE_S = 330; // keep a little over 5 min for backfill windows
const TRIM_SLACK_S = 15; // amortize trims: only front-splice once past this overshoot
// wall-clock slack on top of maxAge before receipt-based staleness trips —
// covers frame batching / network jitter without flapping on ~1 Hz groups
const RECEIPT_SLACK_MS = 1500;

class DataBuffers {
  private t: Map<string, number[]> = new Map();
  // values may contain null gap sentinels (inserted on WS outage) so uPlot
  // draws a hole instead of a fabricated straight line across the outage
  private v: Map<string, (number | null)[]> = new Map();
  // CLIENT RECEIPT wall clock (Date.now) of the last WS append per tag, plus
  // the last frame overall. Server sample times are never compared against
  // the browser clock, so host/client skew can't fake or hide staleness.
  private recvMs: Map<string, number> = new Map();
  private lastFrameRecvMs = 0;

  appendFrame(f: FrameMsg) {
    const now = Date.now();
    this.lastFrameRecvMs = now;
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
      if (col.length) this.recvMs.set(tag, now);
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
  }

  /** Merge a REST backfill into whatever has already streamed: backfill fills
   * history before the buffer and the inside of outage gaps (null sentinels);
   * streamed samples always win — only overlap is dropped (H9). Safe to call
   * repeatedly (initial load AND every WS reconnect). */
  seed(tag: string, t: number[], v: number[]) {
    if (!t.length) return;
    const curT = this.t.get(tag);
    const curV = this.v.get(tag);
    if (!curT || !curV || curT.length === 0) {
      this.t.set(tag, t.slice());
      this.v.set(tag, v.slice());
      this.trim(tag);
      return;
    }
    // covered intervals = contiguous runs of streamed samples (split at gap
    // sentinels); a backfill point inside one is overlap and gets dropped
    const segs: [number, number][] = [];
    let segStart: number | null = null;
    for (let i = 0; i < curT.length; i++) {
      if (curV[i] === null) {
        if (segStart !== null) segs.push([segStart, curT[i - 1]]);
        segStart = null;
      } else if (segStart === null) {
        segStart = curT[i];
      }
    }
    if (segStart !== null) segs.push([segStart, curT[curT.length - 1]]);
    const eps = 1e-6;
    const covered = (tt: number) => segs.some(([a, b]) => tt >= a - eps && tt <= b + eps);

    const outT: number[] = [];
    const outV: (number | null)[] = [];
    let i = 0; // backfill index
    let j = 0; // existing index
    while (i < t.length || j < curT.length) {
      if (i < t.length && (j >= curT.length || t[i] < curT[j])) {
        if (!covered(t[i])) {
          outT.push(t[i]);
          outV.push(v[i]);
        }
        i++;
      } else {
        outT.push(curT[j]);
        outV.push(curV[j]);
        j++;
      }
    }
    this.t.set(tag, outT);
    this.v.set(tag, outV);
    this.trim(tag);
  }

  /** Insert a null gap sentinel after the newest sample of every tag, so a
   * WS outage renders as a hole rather than a straight line bridging it
   * (H9). Idempotent per outage. */
  markGap() {
    this.t.forEach((ts, tag) => {
      const vs = this.v.get(tag)!;
      if (!ts.length || vs[vs.length - 1] === null) return;
      ts.push(ts[ts.length - 1] + 1e-3);
      vs.push(null);
    });
  }

  private trim(tag: string) {
    const ts = this.t.get(tag)!;
    const vs = this.v.get(tag)!;
    if (ts.length < 2) return;
    const cutoff = ts[ts.length - 1] - MAX_AGE_S;
    // F8: amortize the O(n) front-splice — pay for it only once the buffer
    // overshoots by a chunk, not on every 10 Hz frame for every tag
    if (ts[0] >= cutoff - TRIM_SLACK_S) return;
    let drop = 0;
    while (drop < ts.length && ts[drop] < cutoff) drop++;
    if (drop > 0) {
      ts.splice(0, drop);
      vs.splice(0, drop);
    }
  }

  latest(tag: string): number | null {
    const vs = this.v.get(tag);
    if (!vs) return null;
    for (let i = vs.length - 1; i >= 0; i--) {
      if (vs[i] !== null) return vs[i];
    }
    return null;
  }

  // stale-data guard (phase 15): a dead sensor must not look like a frozen
  // live value. Two conditions, either marks the tag stale:
  //  1. wall-clock ceiling on CLIENT RECEIPT time — catches a total stream
  //     stall where every buffer froze at the same instant (H8). Receipt
  //     times are local Date.now() stamps, so server/client clock skew is
  //     irrelevant by construction.
  //  2. relative check against the freshest tag — catches a single dead
  //     sensor among live ones.
  isStale(tag: string, maxAgeS = 1.0): boolean {
    const ts = this.t.get(tag);
    if (!ts || ts.length === 0) return true;
    const now = Date.now();
    const ceiling = maxAgeS * 1000 + RECEIPT_SLACK_MS;
    const recv = this.recvMs.get(tag);
    if (recv === undefined || now - recv > ceiling) return true; // seeded-only or no live data
    if (now - this.lastFrameRecvMs > ceiling) return true; // no frames at all
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

  // uPlot-aligned data for a set of tags (null-joined where timebases differ).
  aligned(tags: string[]): uPlot.AlignedData {
    const present = tags.filter((tag) => this.t.get(tag)?.length);
    if (present.length === 0) return [[], ...tags.map(() => [])] as unknown as uPlot.AlignedData;
    const joined = uPlot.join(
      present.map(
        (tag) => [this.t.get(tag)!, this.v.get(tag)!] as unknown as uPlot.AlignedData,
      ),
    );
    // re-insert empty series for absent tags to keep series order stable
    const out: (number | null | undefined)[][] = [joined[0] as number[]];
    let j = 1;
    for (const tag of tags) {
      out.push(present.includes(tag) ? (joined[j++] as number[]) : []);
    }
    return out as uPlot.AlignedData;
  }
}

export const buffers = new DataBuffers();

// Chart refresh signal: bump a counter at 10 Hz; panels subscribe via zustand.
export type TickListener = () => void;
const listeners = new Set<TickListener>();
export function onTick(fn: TickListener): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
// F8: don't burn main-thread time repainting a tab nobody is looking at —
// skip ticks while hidden and repaint once immediately on return.
setInterval(() => {
  if (document.hidden) return;
  listeners.forEach((fn) => fn());
}, 100);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) listeners.forEach((fn) => fn());
});
