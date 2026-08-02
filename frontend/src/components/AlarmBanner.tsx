import { resetSequence } from "../api";
import { useStore } from "../store";

// Unmissable strip under the status bar whenever the sequencer is in an
// abnormal state. ABORTING stays red while the abort table runs; ABORTED and
// FAULT persist until the operator resets.
export function AlarmBanner() {
  const seq = useStore((s) => s.seqStatus);
  if (!seq || !["ABORTING", "ABORTED", "FAULT"].includes(seq.state)) return null;

  const detail = seq.error !== "NONE" ? ` — ${seq.error}` : "";
  return (
    <div className={`alarm-banner ${seq.state === "ABORTING" ? "active" : ""}`}>
      <span>
        {seq.state}
        {detail}
        {seq.state === "ABORTING" ? ` · abort step ${seq.cur_step}` : ""}
      </span>
      {seq.state !== "ABORTING" && (
        <button className="btn" onClick={() => resetSequence().catch(() => {})}>
          RESET
        </button>
      )}
    </div>
  );
}
