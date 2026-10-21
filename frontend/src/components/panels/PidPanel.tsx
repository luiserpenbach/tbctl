import { useEffect, useMemo, useState } from "react";
import { fetchPid, setManualValve, type PidConfigT, type PidElement } from "../../api";
import { buffers, onSlowTick } from "../../databuf";
import { useStore } from "../../store";
import type { PanelConfig } from "../../types";
import { PidStage } from "../pid/PidStage";

// Live P&ID mimic + control surface (roadmap phase 13). Valve clicks issue
// the existing /api/manual/valve with the same gating as the valve panel
// (control mode + sequencer IDLE) behind a click-to-confirm popover.

export function PidPanel({ panel }: { panel: PanelConfig }) {
  const status = useStore((s) => s.status);
  const seq = useStore((s) => s.seqStatus);
  const channels = useStore((s) => s.channels);
  const [cfg, setCfg] = useState<PidConfigT | null>(null);
  const [err, setErr] = useState("");
  // element pending a confirm click: {id, action}
  const [pending, setPending] = useState<{ id: string; tag: string; action: string } | null>(null);
  const [, setTick] = useState(0);

  // labels are text: 10 Hz is enough (charts use the per-frame tick)
  useEffect(() => onSlowTick(() => setTick((t) => t + 1)), []);
  useEffect(() => {
    setCfg(null);
    setErr("");
    if (panel.pid)
      fetchPid(panel.pid)
        .then((r) => setCfg(r.config))
        .catch((e) => setErr(String(e)));
  }, [panel.pid]);
  useEffect(() => {
    if (!pending) return;
    const h = setTimeout(() => setPending(null), 3000);
    return () => clearTimeout(h);
  }, [pending]);

  const byTag = useMemo(() => new Map(channels.map((c) => [c.tag, c])), [channels]);

  if (!panel.pid) return <div className="placeholder">no P&ID selected (configure panel)</div>;
  if (err) return <div className="placeholder">{err}</div>;
  if (!cfg || !status) return <div className="placeholder">loading…</div>;

  const seqState = seq?.state ?? status.sequencer.state;
  const canActuate = status.control_enabled && seqState === "IDLE";

  // prefer the 10 Hz WS dio_state over the 2 s status poll (same as valve_panel)
  const liveValve = (tag: string): boolean | null => {
    const c = byTag.get(tag);
    if (!c) return null;
    if (seq && c.bit !== undefined && c.active_level !== undefined)
      return ((seq.dio_state >> c.bit) & 1) === c.active_level;
    const s = status.outputs[tag];
    return s === undefined ? null : s === "open" || s === "on";
  };

  const clickValve = (el: PidElement) => {
    if (el.type !== "valve") return;
    const active = liveValve(el.channel);
    const c = byTag.get(el.channel);
    const action =
      c?.kind === "igniter" ? (active ? "off" : "on") : active ? "close" : "open";
    if (pending?.id === el.id) {
      setPending(null);
      setErr("");
      setManualValve(el.channel, action).catch((e) =>
        setErr(String(e).replace(/^Error: /, "")),
      );
    } else if (canActuate) {
      setPending({ id: el.id, tag: el.channel, action });
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div style={{ flex: 1, position: "relative", minHeight: 0 }}>
        <PidStage
          cfg={cfg}
          valveKind={(tag) => (byTag.get(tag)?.kind === "igniter" ? "igniter" : "valve")}
          liveValve={liveValve}
          liveValue={(tag) => buffers.latest(tag)}
          staleOf={(tag) => buffers.isStale(tag)}
          unitOf={(tag) => byTag.get(tag)?.unit ?? ""}
          onElementClick={clickValve}
        >
          {(box) => {
            if (!pending) return null;
            const el = cfg.elements.find((e) => e.id === pending.id);
            if (!el) return null;
            return (
              <div
                className="pid-confirm"
                style={{ left: box.left + el.x * box.w, top: box.top + el.y * box.h - 26 }}
              >
                {pending.action} {pending.tag.split(".")[1]}? click again
              </div>
            );
          }}
        </PidStage>
      </div>
      {!canActuate && (
        <div style={{ fontSize: 11.5, color: "var(--text-faint)", padding: "2px 8px" }}>
          view-only: {status.control_enabled ? `sequencer ${seqState}` : "control mode disabled"}
        </div>
      )}
      {err && <div style={{ color: "var(--red)", fontSize: 11, padding: "0 8px 4px" }}>{err}</div>}
    </div>
  );
}
