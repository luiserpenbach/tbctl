import { useState } from "react";
import { setManualValve } from "../../api";
import { useStore } from "../../store";

// Manual buttons are only live when control mode is enabled AND the
// sequencer is IDLE (§8); the node enforces the same rule server-side.
export function ValvePanel() {
  const status = useStore((s) => s.status);
  const seq = useStore((s) => s.seqStatus);
  const channels = useStore((s) => s.channels);
  const [err, setErr] = useState("");
  const outputs = channels.filter((c) => c.kind === "valve" || c.kind === "igniter");

  if (!status) return <div className="placeholder">waiting for status…</div>;

  const seqState = seq?.state ?? status.sequencer.state;
  const canActuate = status.control_enabled && seqState === "IDLE";

  const actuate = (tag: string, state: string) => {
    setErr("");
    setManualValve(tag, state).catch((e) => setErr(String(e).replace(/^Error: /, "")));
  };

  // prefer the 10 Hz WS dio_state over the 2 s status poll
  const liveState = (c: (typeof outputs)[number]): string => {
    if (seq && c.bit !== undefined && c.active_level !== undefined) {
      const level = (seq.dio_state >> c.bit) & 1;
      const active = level === c.active_level;
      return c.kind === "valve" ? (active ? "open" : "closed") : active ? "on" : "off";
    }
    return status.outputs[c.tag] ?? "—";
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <div className="valve-grid" style={{ flex: 1, overflowY: "auto" }}>
        {outputs.map((c) => {
          const state = liveState(c);
          const isValve = c.kind === "valve";
          const [aLabel, bLabel] = isValve ? ["open", "close"] : ["on", "off"];
          const activeNow = state === "open" || state === "on";
          return (
            <div key={c.tag} className={`valve-cell ${state}`} title={`${c.tag} · ${c.hw} (bit ${c.bit})`}>
              <div className="vhead">
                <span className="name">{c.name || c.tag}</span>
                <span className="led" />
              </div>
              <div className="state">{state.toUpperCase()}</div>
              <div className="sub">{c.name ? c.tag : c.hw}</div>
              <div className="valve-btns">
                <button
                  className="btn"
                  disabled={!canActuate || activeNow}
                  onClick={() => actuate(c.tag, aLabel)}
                >
                  {aLabel}
                </button>
                <button
                  className="btn"
                  disabled={!canActuate || !activeNow}
                  onClick={() => actuate(c.tag, bLabel)}
                >
                  {bLabel}
                </button>
              </div>
            </div>
          );
        })}
      </div>
      {!canActuate && (
        <div className="panel-foot">
          <svg className="lock" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <rect x="5" y="11" width="14" height="9" rx="2" />
            <path d="M8 11V8a4 4 0 0 1 8 0v3" />
          </svg>
          {status.control_enabled ? `Locked while sequencer is ${seqState}` : "Manual control locked — switch Control to Live"}
        </div>
      )}
      {err && <div className="err-text" style={{ padding: "0 12px 6px" }}>{err}</div>}
    </div>
  );
}
