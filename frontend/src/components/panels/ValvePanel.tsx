import { useState } from "react";
import { setManualValve } from "../../api";
import { useStore } from "../../store";

// dio_state older than this is not trusted for valve rendering/gating (M28)
const SEQ_FRESH_MS = 2500;

// Manual buttons are only live when control mode is enabled AND the
// sequencer is IDLE (§8); the node enforces the same rule server-side.
export function ValvePanel() {
  const status = useStore((s) => s.status);
  const seq = useStore((s) => s.seqStatus);
  const lastSeqStatusAt = useStore((s) => s.lastSeqStatusAt);
  const channels = useStore((s) => s.channels);
  const [err, setErr] = useState("");
  const outputs = channels.filter((c) => c.kind === "valve" || c.kind === "spark");

  if (!status) return <div className="placeholder">waiting for status…</div>;

  // M28: prefer the 10 Hz WS dio_state, but only while it is actually
  // fresh — during a WS outage fall back to the 2 s REST status poll
  const seqFresh = seq !== null && Date.now() - lastSeqStatusAt < SEQ_FRESH_MS;
  const seqState = seqFresh && seq ? seq.state : status.sequencer.state;
  const canActuate = status.control_enabled && seqState === "IDLE";

  const actuate = (tag: string, state: string) => {
    setErr("");
    setManualValve(tag, state).catch((e) => setErr(String(e).replace(/^Error: /, "")));
  };

  const liveState = (c: (typeof outputs)[number]): string => {
    if (seqFresh && seq && c.bit !== undefined && c.active_level !== undefined) {
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
            <div key={c.tag} className={`valve-cell ${state}`} title={`${c.hw} (bit ${c.bit})`}>
              <div className="name">{c.tag}</div>
              <div className="state">{state.toUpperCase()}</div>
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
        <div style={{ fontSize: 10, color: "var(--text-faint)", padding: "2px 8px" }}>
          {status.control_enabled ? `locked: sequencer ${seqState}` : "control mode disabled"}
        </div>
      )}
      {err && <div style={{ color: "var(--red)", fontSize: 11, padding: "0 8px 4px" }}>{err}</div>}
    </div>
  );
}
