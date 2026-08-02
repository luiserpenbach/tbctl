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
  const tags = panel.channels;
  const isHistory = viewMode.mode === "run";
  const markersRef = useRef<Marker[]>([]);

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

    const opts: uPlot.Options = {
      width: el.clientWidth,
      height: el.clientHeight,
      cursor: {
        sync: { key: SYNC_KEY.key },
        drag: { x: true, y: false },
      },
      scales: {
        x: { time: true },
        y:
          panel.y_axis.mode === "manual"
            ? { auto: false, range: [panel.y_axis.min ?? 0, panel.y_axis.max ?? 1] }
            : { auto: true },
      },
      axes: [{ ...AXIS }, { ...AXIS, size: 52 }],
      series: [
        {},
        ...tags.map((tag, i) => ({
          label: tag,
          stroke: PALETTE[i % PALETTE.length],
          width: 1.4,
          points: { show: false },
        })),
      ],
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

    const plot = new uPlot(opts, [[], ...tags.map(() => [])] as unknown as uPlot.AlignedData, el);
    plotRef.current = plot;

    const ro = new ResizeObserver(() => {
      plot.setSize({ width: el.clientWidth, height: el.clientHeight });
    });
    ro.observe(el);

    let off = () => {};
    if (isHistory && viewMode.mode === "run") {
      markersRef.current = [];
      fetchRunEvents(viewMode.run.run_id)
        .then((r) => {
          markersRef.current = r.events
            .map(toMarker)
            .filter((m): m is Marker => m !== null);
          plot.redraw();
        })
        .catch(() => {});
      // historical: fetch the run's data once and frame the whole run
      fetchRunData(viewMode.run.run_id, tags, 4000)
        .then((r) => {
          const present = tags.filter((tag) => r.series[tag]?.t.length);
          if (!present.length) return;
          const joined = uPlot.join(
            present.map((tag) => [r.series[tag].t, r.series[tag].mean] as uPlot.AlignedData),
          );
          const data: (number | null | undefined)[][] = [joined[0] as number[]];
          let j = 1;
          for (const tag of tags) {
            data.push(present.includes(tag) ? (joined[j++] as number[]) : []);
          }
          plot.setData(data as uPlot.AlignedData);
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
          plot.setScale("x", { min: latest - DEFAULT_WINDOW_S, max: latest });
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
    isHistory,
    viewMode.mode === "run" ? viewMode.run.run_id : "",
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
      <div className="series-chips">
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
        {!follow && !isHistory && (
          <button
            className="btn follow-btn"
            onClick={() => {
              followRef.current = true;
              setFollow(true);
            }}
          >
            ▶ LIVE
          </button>
        )}
        <div ref={elRef} style={{ width: "100%", height: "100%" }} />
      </div>
    </div>
  );
}
