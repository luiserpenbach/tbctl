import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import { fetchLive, fetchRunData, fetchRunEvents } from "../../api";
import { buffers, onTick } from "../../databuf";
import { useStore } from "../../store";
import type { PanelConfig } from "../../types";

// Sift-like series colors: vivid, high-contrast on black
const PALETTE = [
  "#3fd158",
  "#4a9eff",
  "#e26ee8",
  "#f0883e",
  "#e3b341",
  "#39c5cf",
  "#f778ba",
  "#8b949e",
];
const AXIS = {
  stroke: "#9e9ea4",
  font: "10px 'IBM Plex Mono', monospace",
  grid: { stroke: "#1b1b1b", width: 1 },
  ticks: { stroke: "#262626" },
};

const SYNC_KEY = uPlot.sync("tbctl");
const DEFAULT_WINDOW_S = 60;
const runShort = (runId: string) => runId.replace(/^\d{8}-/, "");

const WINDOW_CHOICES: [number, string][] = [
  [10, "10s"],
  [30, "30s"],
  [60, "1m"],
  [300, "5m"],
];

// run-review event markers (phase 15): what to draw and how
interface Marker {
  t: number;
  letter: string;
  color: string;
  label: string;
}

function toMarker(e: { event: string; time: number; [k: string]: unknown }): Marker | null {
  switch (e.event) {
    case "note":
      return { t: e.time, letter: "N", color: "#4cc9f0", label: String(e.text ?? "note") };
    case "seq_fired":
      return { t: e.time, letter: "F", color: "#2ecc71", label: `FIRE ${e.sequence ?? ""}` };
    case "seq_abort_commanded":
      return { t: e.time, letter: "A", color: "#e74c3c", label: "abort commanded" };
    case "seq_terminal":
      return e.state === "ABORTED" || e.state === "FAULT"
        ? { t: e.time, letter: "A", color: "#e74c3c", label: `${e.state} ${e.error ?? ""}` }
        : null;
    case "redline_tripped":
      return { t: e.time, letter: "R", color: "#f1c40f", label: String(e.reason ?? "redline") };
    default:
      return null;
  }
}

export function TimeseriesPanel({ panel }: { panel: PanelConfig }) {
  const elRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const followRef = useRef(true);
  const [follow, setFollow] = useState(true);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const hiddenRef = useRef(hidden);
  hiddenRef.current = hidden;
  const channels = useStore((s) => s.channels);
  const viewMode = useStore((s) => s.viewMode);
  const overlayRuns = useStore((s) => s.overlayRuns);
  const toggleOverlayRun = useStore((s) => s.toggleOverlayRun);
  const tags = panel.channels;
  const isHistory = viewMode.mode === "run";
  const markersRef = useRef<Marker[]>([]);
  const [windowS, setWindowS] = useState(panel.window_s ?? DEFAULT_WINDOW_S);
  const windowRef = useRef(windowS);
  windowRef.current = windowS;
  useEffect(() => setWindowS(panel.window_s ?? DEFAULT_WINDOW_S), [panel.window_s]);

  // one-time backfill of the ring-buffer window (live mode)
  useEffect(() => {
    if (!tags.length || isHistory) return;
    fetchLive(tags, 300, 2000)
      .then((r) => {
        for (const tag of tags) {
          const s = r.series[tag];
          if (s) buffers.seed(tag, s.t, s.mean);
        }
      })
      .catch(() => {});
  }, [tags, isHistory]);

  useEffect(() => {
    const el = elRef.current;
    if (!el || !tags.length) return;

    // run view: primary run + FIRE-aligned overlays (t=0 at seq_fired)
    const runsToPlot =
      isHistory && viewMode.mode === "run"
        ? [viewMode.run, ...overlayRuns.filter((o) => o.run_id !== viewMode.run.run_id)]
        : [];
    const relative = runsToPlot.length > 1;

    const seriesDefs =
      runsToPlot.length > 0
        ? runsToPlot.flatMap((run, ri) =>
            tags.map((tag, i) => ({
              label: ri === 0 ? tag : `${tag} · ${runShort(run.run_id)}`,
              stroke: PALETTE[i % PALETTE.length],
              width: ri === 0 ? 1.6 : 1.1,
              dash: ri === 0 ? undefined : ([6, 4] as number[]),
              alpha: ri === 0 ? 1 : 0.75,
              points: { show: false },
            })),
          )
        : tags.map((tag, i) => ({
            label: tag,
            stroke: PALETTE[i % PALETTE.length],
            width: 1.4,
            points: { show: false },
          }));
    const nSeries = seriesDefs.length;

    const opts: uPlot.Options = {
      width: el.clientWidth,
      height: el.clientHeight,
      cursor: {
        sync: { key: SYNC_KEY.key },
        drag: { x: true, y: false },
      },
      scales: {
        x: { time: !relative },
        y: {
          ...(panel.y_axis.mode === "manual"
            ? { auto: false, range: [panel.y_axis.min ?? 0, panel.y_axis.max ?? 1] as [number, number] }
            : { auto: true }),
          ...(panel.y_axis.log ? { distr: 3 as const, log: 10 as const } : {}),
        },
      },
      axes: [
        { ...AXIS },
        {
          ...AXIS,
          size: 52,
          ...(panel.y_axis.label
            ? {
                label: panel.y_axis.label,
                labelFont: "10px 'IBM Plex Mono', monospace",
                labelGap: 4,
                labelSize: 14,
              }
            : {}),
        },
      ],
      series: [{}, ...seriesDefs],
      hooks: {
        setSelect: [
          (u) => {
            // drag-zoom: leave live-follow
            if (u.select.width > 10) {
              followRef.current = false;
              setFollow(false);
            }
          },
        ],
        draw: [
          (u) => {
            // event markers over the plot area (run review, phase 15)
            if (!markersRef.current.length) return;
            const { ctx } = u;
            const top = u.bbox.top;
            const bottom = u.bbox.top + u.bbox.height;
            ctx.save();
            ctx.font = `${10 * devicePixelRatio}px monospace`;
            ctx.textAlign = "center";
            for (const m of markersRef.current) {
              const x = u.valToPos(m.t, "x", true);
              if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) continue;
              ctx.strokeStyle = m.color;
              ctx.setLineDash([4 * devicePixelRatio, 4 * devicePixelRatio]);
              ctx.lineWidth = devicePixelRatio;
              ctx.beginPath();
              ctx.moveTo(x, top);
              ctx.lineTo(x, bottom);
              ctx.stroke();
              ctx.setLineDash([]);
              ctx.fillStyle = m.color;
              ctx.fillText(m.letter, x, top + 10 * devicePixelRatio);
            }
            ctx.restore();
          },
        ],
      },
    };

    const plot = new uPlot(
      opts,
      [[], ...Array.from({ length: nSeries }, () => [])] as unknown as uPlot.AlignedData,
      el,
    );
    plotRef.current = plot;

    const ro = new ResizeObserver(() => {
      plot.setSize({ width: el.clientWidth, height: el.clientHeight });
    });
    ro.observe(el);

    let off = () => {};
    if (isHistory && viewMode.mode === "run") {
      markersRef.current = [];
      // fetch every run's data + events; t0 per run = last seq_fired
      // (fallback: run start) so overlays align on FIRE
      Promise.all(
        runsToPlot.map(async (run) => {
          const [ev, dat] = await Promise.all([
            fetchRunEvents(run.run_id).catch(() => ({ events: [] as { event: string; time: number }[] })),
            fetchRunData(run.run_id, tags, 4000).catch(
              () => ({ run_id: run.run_id, series: {} }) as Awaited<ReturnType<typeof fetchRunData>>,
            ),
          ]);
          let t0 = run.t_start;
          for (const e of ev.events) if (e.event === "seq_fired") t0 = e.time;
          return { run, t0, series: dat.series, events: ev.events };
        }),
      )
        .then((items) => {
          const tables: uPlot.AlignedData[] = [];
          const present: boolean[] = [];
          for (const it of items) {
            for (const tag of tags) {
              const s = it.series[tag];
              if (s?.t.length) {
                const ts = relative ? s.t.map((x) => x - it.t0) : s.t;
                tables.push([ts, s.mean] as uPlot.AlignedData);
                present.push(true);
              } else {
                present.push(false);
              }
            }
          }
          if (tables.length) {
            const joined = uPlot.join(tables);
            const data: (number | null | undefined)[][] = [joined[0] as number[]];
            let j = 1;
            for (const p of present) data.push(p ? (joined[j++] as number[]) : []);
            plot.setData(data as uPlot.AlignedData);
          }
          // event markers come from the primary run only
          const prim = items[0];
          markersRef.current = prim.events
            .map(toMarker)
            .filter((m): m is Marker => m !== null)
            .map((m) => (relative ? { ...m, t: m.t - prim.t0 } : m));
          plot.redraw();
        })
        .catch(() => {});
    } else {
      markersRef.current = [];
      off = onTick(() => {
        const data = buffers.aligned(tags);
        // apply series visibility
        tags.forEach((tag, i) => plot.setSeries(i + 1, { show: !hiddenRef.current.has(tag) }));
        plot.setData(data, false);
        const latest = buffers.latestTime();
        if (followRef.current && latest !== null) {
          plot.setScale("x", { min: latest - windowRef.current, max: latest });
        }
      });
    }

    return () => {
      off();
      ro.disconnect();
      plot.destroy();
      plotRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    tags.join("|"),
    panel.y_axis.mode,
    panel.y_axis.min,
    panel.y_axis.max,
    panel.y_axis.log,
    panel.y_axis.label,
    isHistory,
    viewMode.mode === "run" ? viewMode.run.run_id : "",
    overlayRuns.map((o) => o.run_id).join("|"),
  ]);

  const toggleTag = (tag: string) =>
    setHidden((s) => {
      const next = new Set(s);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });

  const unitOf = (tag: string) => channels.find((c) => c.tag === tag)?.unit ?? "";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {isHistory && viewMode.mode === "run" && overlayRuns.length > 0 && (
        <div className="series-chips">
          <span className="series-chip" title="primary run — solid lines, t=0 at FIRE">
            — {runShort(viewMode.run.run_id)}
          </span>
          {overlayRuns.map((o) => (
            <span
              key={o.run_id}
              className="series-chip"
              style={{ color: "var(--amber)", cursor: "pointer" }}
              title="overlay (dashed, FIRE-aligned) — click to remove"
              onClick={() => toggleOverlayRun(o)}
            >
              ┄ {runShort(o.run_id)} ×
            </span>
          ))}
        </div>
      )}
      <div className="series-chips" style={panel.show_legend === false ? { display: "none" } : {}}>
        {tags.map((tag, i) => (
          <span
            key={tag}
            className={`series-chip ${hidden.has(tag) ? "off" : ""}`}
            onClick={() => toggleTag(tag)}
            title={unitOf(tag)}
          >
            <span className="swatch" style={{ background: PALETTE[i % PALETTE.length] }} />
            {tag}
            {unitOf(tag) && <span className="legend-unit">({unitOf(tag)})</span>}
          </span>
        ))}
      </div>
      <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
        {isHistory && viewMode.mode === "run" && (
          <span className="chip" style={{ position: "absolute", top: 4, left: 6, zIndex: 5 }}>
            {viewMode.run.run_id}
          </span>
        )}
        {!isHistory && (
          <div className="chart-toolbar">
            {WINDOW_CHOICES.map(([s, label]) => (
              <button
                key={s}
                className={`win-btn ${windowS === s ? "active" : ""}`}
                onClick={() => {
                  setWindowS(s);
                  followRef.current = true;
                  setFollow(true);
                }}
              >
                {label}
              </button>
            ))}
            {!follow && (
              <button
                className="win-btn live"
                onClick={() => {
                  followRef.current = true;
                  setFollow(true);
                }}
              >
                ▶ LIVE
              </button>
            )}
          </div>
        )}
        <div ref={elRef} style={{ width: "100%", height: "100%" }} />
      </div>
    </div>
  );
}
