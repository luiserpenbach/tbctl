import { useStore } from "../../store";

// M3 placeholder: live sequencer state mirror. The full LOAD/ARM/FIRE flow
// with 2-step confirms and the persistent ABORT button lands in M5 with the
// sequencer REST endpoints.
export function SequencePanel() {
  const seq = useStore((s) => s.seqStatus);
  return (
    <div className="placeholder" style={{ flexDirection: "column", gap: 6 }}>
      <div>
        sequencer: <b>{seq?.state ?? "—"}</b>
        {seq && seq.error !== "NONE" ? ` (${seq.error})` : ""}
      </div>
      <div style={{ fontSize: 11 }}>load / arm / fire controls arrive in M5</div>
    </div>
  );
}
