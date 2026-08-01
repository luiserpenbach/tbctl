import { useStore } from "../../store";

// M3 placeholder: recording state readout. Start/stop + run metadata form
// arrive with the recorder service (M4).
export function RecorderPanel() {
  const status = useStore((s) => s.status);
  const rec = status?.recording;
  return (
    <div className="placeholder" style={{ flexDirection: "column", gap: 6 }}>
      <div>
        recording: <b>{rec ? (rec.active ? `RUN ${rec.run_id}` : "idle") : "—"}</b>
      </div>
      <div style={{ fontSize: 11 }}>start/stop controls arrive in M4</div>
    </div>
  );
}
