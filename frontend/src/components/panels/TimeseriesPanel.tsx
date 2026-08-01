import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import { fetchLive, fetchRunData } from "../../api";
import { buffers, onTick } from "../../databuf";
import { useStore } from "../../store";
import type { PanelConfig } from "../../types";

const PALETTE = [
  "#4cc9f0",
  "#f72585",
  "#ffd166",
  "#06d6a0",
  "#a78bfa",
  "#ff924c",
  "#43aa8b",
  "#ef476f",
];

const SYNC_KEY = uPlot.sync("tbctl");
const DEFAULT_WINDOW_S = 60;

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
      axes: [
        {
          stroke: "#7d8794",
          grid: { stroke: "#1c2330", width: 1 },
          ticks: { stroke: "#232a36" },
        },
        {
          stroke: "#7d8794",
          grid: { stroke: "#1c2330", width: 1 },
          ticks: { stroke: "#232a36" },
          size: 52,
        },
      ],
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
            style={{ color: PALETTE[i % PALETTE.length] }}
            onClick={() => toggleTag(tag)}
            title={unitOf(tag)}
          >
            {tag}
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
