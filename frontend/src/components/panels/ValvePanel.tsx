import { useStore } from "../../store";

// M3: read-only state indicators. Manual actuation buttons arrive with the
// control-mode endpoints (M5) — buttons only light when control is enabled
// AND the sequencer is IDLE.
export function ValvePanel() {
  const status = useStore((s) => s.status);
  const channels = useStore((s) => s.channels);
  const outputs = channels.filter((c) => c.kind === "valve" || c.kind === "spark");

  if (!status) return <div className="placeholder">waiting for status…</div>;

  return (
    <div className="valve-grid">
      {outputs.map((c) => {
        const state = status.outputs[c.tag] ?? "—";
        return (
          <div key={c.tag} className={`valve-cell ${state}`} title={`${c.hw} (bit ${c.bit})`}>
            <div className="name">{c.tag}</div>
            <div className="state">{state.toUpperCase()}</div>
          </div>
        );
      })}
    </div>
  );
}
