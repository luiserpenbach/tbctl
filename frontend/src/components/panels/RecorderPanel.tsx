import { useEffect, useState, type CSSProperties } from "react";
import { fetchRuns, startRun, stopRun } from "../../api";
import { useStore } from "../../store";

export function RecorderPanel() {
  const status = useStore((s) => s.status);
  const setRuns = useStore((s) => s.setRuns);
  const [name, setName] = useState("");
  const [operator, setOperator] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [, setClock] = useState(0);

  useEffect(() => {
    const iv = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(iv);
  }, []);

  const rec = status?.recording;
  const elapsed =
    rec?.active && rec.t_start ? Math.max(0, (Date.now() / 1000 - rec.t_start) | 0) : 0;

  const doStart = async () => {
    setBusy(true);
    setErr("");
    try {
      await startRun(name || "run", operator || "unknown", notes);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const doStop = async () => {
    setBusy(true);
    setErr("");
    try {
      await stopRun();
      fetchRuns().then((r) => setRuns(r.runs));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const inputStyle: CSSProperties = {
    background: "var(--bg)",
    border: "1px solid var(--border)",
    borderRadius: 3,
    color: "var(--text)",
    padding: "4px 8px",
    fontSize: 12,
    width: "100%",
  };

  return (
    <div style={{ padding: 10, display: "flex", flexDirection: "column", gap: 6, height: "100%" }}>
      {rec?.active ? (
        <>
          <div style={{ fontFamily: "var(--mono)", fontSize: 13 }}>
            <span style={{ color: "var(--red)" }}>●</span> {rec.run_id}
          </div>
          <div style={{ fontFamily: "var(--mono)", fontSize: 22 }}>
            {new Date(elapsed * 1000).toISOString().slice(11, 19)}
          </div>
          <button className="btn" onClick={doStop} disabled={busy} style={{ marginTop: "auto" }}>
            ■ stop run
          </button>
        </>
      ) : (
        <>
          <input style={inputStyle} placeholder="run name" value={name} onChange={(e) => setName(e.target.value)} />
          <input style={inputStyle} placeholder="operator" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <input style={inputStyle} placeholder="notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
          <button className="btn" onClick={doStart} disabled={busy} style={{ marginTop: "auto" }}>
            ● start run
          </button>
        </>
      )}
      {err && <div style={{ color: "var(--red)", fontSize: 11 }}>{err}</div>}
    </div>
  );
}
