import { useEffect, useState } from "react";
import { fetchRunEvents } from "../api";
import { useStore } from "../store";
import type { EventRecord } from "../types";

// Collapsible time-ordered event lane for the selected run (tier 2):
// valve commands, sequence lifecycle, notes, aborts — with +t offsets so
// review reads like a timeline. Colors match the chart markers.

const COLOR: Record<string, string> = {
  seq_fired: "var(--green)",
  seq_abort_commanded: "var(--red)",
  seq_terminal: "var(--red)",
  redline_tripped: "var(--amber)",
  note: "var(--accent)",
};

function eventText(e: EventRecord): string {
  const extras = Object.entries(e)
    .filter(([k]) => !["event", "time", "iso", "run_id"].includes(k))
    .map(([, v]) => String(v))
    .join(" ");
  return `${e.event} ${extras}`.trim();
}

export function RunEventsBar() {
  const viewMode = useStore((s) => s.viewMode);
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [open, setOpen] = useState(false);
  const runId = viewMode.mode === "run" ? viewMode.run.run_id : null;
  const tStart = viewMode.mode === "run" ? viewMode.run.t_start : 0;

  useEffect(() => {
    setEvents([]);
    setOpen(false);
    if (!runId) return;
    // stale-guard (M22): a slow response for a previous run must not land
    // after switching to another run (or back to live)
    let cancelled = false;
    fetchRunEvents(runId)
      .then((r) => {
        if (!cancelled) setEvents(r.events);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [runId]);

  if (!runId || events.length === 0) return null;

  return (
    <div className="report-bar" style={{ marginTop: -4 }}>
      <button className="btn" style={{ padding: "0 8px", fontSize: 9.5 }} onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} Events ({events.length})
      </button>
      {(open ? events : events.filter((e) => e.event in COLOR)).map((e, i) => (
        <span
          key={i}
          className="ann-row"
          style={{ color: COLOR[e.event] ?? "var(--text-dim)" }}
          title={new Date(e.time * 1000).toISOString()}
        >
          +{Math.max(0, e.time - tStart).toFixed(1)}s {eventText(e).slice(0, 60)}
        </span>
      ))}
    </div>
  );
}
