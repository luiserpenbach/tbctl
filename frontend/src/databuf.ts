// Live sample buffers, kept OUTSIDE React state: WS frames arrive at ~10 Hz
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

  // uPlot-aligned data for a set of tags (null-joined where timebases differ).
  aligned(tags: string[]): uPlot.AlignedData {
    const present = tags.filter((tag) => this.t.get(tag)?.length);
    if (present.length === 0) return [[], ...tags.map(() => [])] as unknown as uPlot.AlignedData;
    const joined = uPlot.join(
      present.map((tag) => [this.t.get(tag)!, this.v.get(tag)!] as uPlot.AlignedData),
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
setInterval(() => listeners.forEach((fn) => fn()), 100);
