import { useEffect } from "react";
import { fetchEvents } from "../api";
import { useStore } from "../store";
import type { EventRecord } from "../types";

const ALARM = new Set(["seq_abort_commanded", "seq_terminal", "seq_load_failed", "seq_verify_failed"]);

function describe(e: EventRecord): string {
  const extra = Object.entries(e)
    .filter(([k]) => !["time", "iso", "event", "type"].includes(k))
    .map(([, v]) => String(v))
    .join(" ");
  return extra ? `${e.event} ${extra}` : e.event;
}

export function EventFeed() {
  const events = useStore((s) => s.events);
  const setEvents = useStore((s) => s.setEvents);

  useEffect(() => {
    fetchEvents(50)
      .then((r) => setEvents(r.events))
      .catch(() => {});
  }, [setEvents]);

  return (
    <div className="event-feed">
      <div className="group-header">events</div>
      {events.length === 0 && <div className="event-row dim">no events yet</div>}
      {events.slice(0, 30).map((e, i) => (
        <div key={`${e.time}-${i}`} className={`event-row ${ALARM.has(e.event) ? "alarm" : ""}`}>
          <span className="event-time">
            {new Date(e.time * 1000).toLocaleTimeString(undefined, { hour12: false })}
          </span>
          <span className="event-text" title={describe(e)}>
            {describe(e)}
          </span>
        </div>
      ))}
    </div>
  );
}
