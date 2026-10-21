import { useEffect, useState } from "react";
import { post } from "../../api";
import { useStore } from "../../store";

// Simulator setpoint controls — only meaningful when the node's device is
// the mock. Used for operator training and redline/precondition demos.
export function SimPanel() {
  const status = useStore((s) => s.status);
  const channels = useStore((s) => s.channels);
  const [setpoints, setSetpoints] = useState<Record<string, number>>({});
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [err, setErr] = useState("");

  const isSim = status?.device.sim ?? false;

  useEffect(() => {
    if (!isSim) return;
    fetch("/api/sim/setpoints")
      .then((r) => r.json())
      .then((d) => setSetpoints(d.setpoints ?? {}))
      .catch(() => {});
  }, [isSim]);

  if (!isSim) {
    return <div className="placeholder">sim controls (node is not in simulator mode)</div>;
  }

  const inputs = channels.filter((c) => c.kind === "analog_in" || c.kind === "thermocouple_k");

  const apply = async (tag: string) => {
    const v = parseFloat(edits[tag] ?? "");
    if (!Number.isFinite(v)) return;
    setErr("");
    try {
      const r = (await post("/api/sim/set", { tag, value: v })) as {
        setpoints: Record<string, number>;
      };
      setSetpoints(r.setpoints);
    } catch (e) {
      setErr(String(e).replace(/^Error: /, ""));
    }
  };

  return (
    <div style={{ overflowY: "auto", height: "100%" }}>
      <table className="value-table">
        <tbody>
          {inputs.map((c) => (
            <tr key={c.tag}>
              <td>{c.tag}</td>
              <td className="val">{(setpoints[c.tag] ?? 0).toFixed(2)}</td>
              <td className="unit">{c.unit}</td>
              <td style={{ width: 110 }}>
                <input
                  className="modal-input"
                  style={{ width: 70, padding: "1px 6px", fontSize: 11 }}
                  placeholder="new"
                  value={edits[c.tag] ?? ""}
                  onChange={(e) => setEdits({ ...edits, [c.tag]: e.target.value })}
                  onKeyDown={(e) => e.key === "Enter" && apply(c.tag)}
                />
                <button className="btn" style={{ marginLeft: 4 }} onClick={() => apply(c.tag)}>
                  set
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {err && <div style={{ color: "var(--red)", fontSize: 11, padding: 6 }}>{err}</div>}
    </div>
  );
}
