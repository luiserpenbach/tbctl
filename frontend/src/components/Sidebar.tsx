import { useEffect, useMemo, useState } from "react";
import { fetchRuns, runCsvUrl, runFileUrl } from "../api";
import { buffers, onTick } from "../databuf";
import { useStore } from "../store";
import { isReadable } from "../types";
import { EventFeed } from "./EventFeed";

export function Sidebar() {
  const channels = useStore((s) => s.channels);
  const status = useStore((s) => s.status);
  const runs = useStore((s) => s.runs);
  const setRuns = useStore((s) => s.setRuns);
  const viewMode = useStore((s) => s.viewMode);
  const setViewMode = useStore((s) => s.setViewMode);
  const overlayRuns = useStore((s) => s.overlayRuns);
  const toggleOverlayRun = useStore((s) => s.toggleOverlayRun);
  const [query, setQuery] = useState("");
  const [runQuery, setRunQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [, setTick] = useState(0);

  useEffect(() => onTick(() => setTick((t) => t + 1)), []);
  useEffect(() => {
    fetchRuns()
      .then((r) => setRuns(r.runs))
      .catch(() => {});
  }, [setRuns]);

  const groups = useMemo(() => {
    const q = query.toLowerCase();
    const filtered = channels.filter((c) => c.tag.toLowerCase().includes(q));
    const by = new Map<string, typeof filtered>();
    for (const c of filtered) {
      const ns = c.tag.split(".")[0];
      if (!by.has(ns)) by.set(ns, []);
      by.get(ns)!.push(c);
    }
    return [...by.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [channels, query]);

  const toggle = (ns: string) =>
    setCollapsed((s) => {
      const next = new Set(s);
      if (next.has(ns)) next.delete(ns);
      else next.add(ns);
      return next;
    });

  return (
    <aside className="sidebar">
      <input
        className="search"
        placeholder="Filter channels…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="tree">
        <div className="group-header">runs ({runs.length})</div>
        {runs.length > 5 && (
          <input
            className="search"
            style={{ margin: "2px 4px 4px", width: "calc(100% - 8px)" }}
            placeholder="Filter runs (name, tag, outcome)…"
            value={runQuery}
            onChange={(e) => setRunQuery(e.target.value)}
          />
        )}
        <div
          className="chan"
          style={{ cursor: "pointer" }}
          onClick={() => setViewMode({ mode: "live" })}
        >
          <span style={{ color: viewMode.mode === "live" ? "var(--accent)" : undefined }}>
            ▶ live
          </span>
        </div>
        {runs
          .filter((r) => {
            const q = runQuery.toLowerCase().trim();
            if (!q) return true;
            return (
              r.run_id.toLowerCase().includes(q) ||
              (r.tags ?? []).some((t) => t.toLowerCase().includes(q)) ||
              (r.outcome ?? "").includes(q) ||
              r.operator.toLowerCase().includes(q)
            );
          })
          .slice(0, 20)
          .map((r) => {
          const active = viewMode.mode === "run" && viewMode.run.run_id === r.run_id;
          const overlaid = overlayRuns.some((o) => o.run_id === r.run_id);
          const outcomeColor =
            r.outcome === "good"
              ? "var(--green)"
              : r.outcome === "anomaly"
                ? "var(--red)"
                : r.outcome === "scrub"
                  ? "var(--text-faint)"
                  : null;
          return (
            <div
              key={r.run_id}
              className="chan"
              style={{ cursor: "pointer" }}
              title={`${r.operator} — ${r.notes}${r.tags?.length ? ` · ${r.tags.join(", ")}` : ""}`}
              onClick={() => setViewMode({ mode: "run", run: r })}
            >
              <span style={{ color: active ? "var(--accent)" : overlaid ? "var(--amber)" : undefined }}>
                {outcomeColor && (
                  <span style={{ color: outcomeColor, marginRight: 4 }} title={r.outcome ?? ""}>
                    ●
                  </span>
                )}
                {r.run_id}
              </span>
              {viewMode.mode === "run" && !active && (
                <a
                  className="csv-link"
                  title={overlaid ? "remove overlay" : "overlay on charts (FIRE-aligned)"}
                  style={overlaid ? { color: "var(--amber)" } : {}}
                  onClick={(e) => {
                    e.stopPropagation();
                    toggleOverlayRun(r);
                  }}
                >
                  {overlaid ? "⊖" : "⊕"}
                </a>
              )}
              <a
                className="csv-link"
                title="download HDF5 (Metron ingest)"
                href={runFileUrl(r.run_id)}
                download
                onClick={(e) => e.stopPropagation()}
              >
                h5⬇
              </a>
              <a
                className="csv-link"
                title="download CSV (all input channels)"
                href={runCsvUrl(
                  r.run_id,
                  channels
                    .filter(isReadable)
                    .map((c) => c.tag),
                )}
                download
                onClick={(e) => e.stopPropagation()}
              >
                csv⬇
              </a>
            </div>
          );
        })}
        {groups.map(([ns, chans]) => (
          <div key={ns}>
            <div className="group-header" onClick={() => toggle(ns)}>
              {collapsed.has(ns) ? "▸" : "▾"} {ns} ({chans.length})
            </div>
            {!collapsed.has(ns) &&
              chans.map((c) => {
                const isOutput = c.kind === "valve" || c.kind === "spark";
                const val = isOutput
                  ? (status?.outputs[c.tag] ?? "—")
                  : fmt(buffers.latest(c.tag));
                return (
                  <div key={c.tag} className={`chan ${isOutput ? "output" : ""}`} title={c.hw}>
                    <span>{c.tag.slice(ns.length + 1)}</span>
                    <span className="val">
                      {val} {c.unit ?? ""}
                    </span>
                  </div>
                );
              })}
          </div>
        ))}
      </div>
      <EventFeed />
    </aside>
  );
}

function fmt(v: number | null): string {
  if (v === null) return "—";
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
}
