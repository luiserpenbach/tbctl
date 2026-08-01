import { useEffect, useState } from "react";
import { fetchStatus, saveDashboard, setControlMode } from "../api";
import { useStore } from "../store";

export function StatusBar() {
  const status = useStore((s) => s.status);
  const seq = useStore((s) => s.seqStatus);
  const wsConnected = useStore((s) => s.wsConnected);
  const lastSeqAt = useStore((s) => s.lastSeqStatusAt);
  const editMode = useStore((s) => s.editMode);
  const setEditMode = useStore((s) => s.setEditMode);
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const dashboard = useStore((s) => s.dashboard);

  // re-render each second for heartbeat-age evaluation
  const [, setNow] = useState(0);
  useEffect(() => {
    const iv = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(iv);
  }, []);

  const deviceOk = status?.device.connected ?? false;
  const hbFresh = wsConnected && Date.now() - lastSeqAt < 2000;
  const seqState = seq?.state ?? status?.sequencer.state ?? "—";
  const recording = status?.recording.active ?? false;

  return (
    <header className="statusbar">
      <button className="btn" onClick={toggleSidebar} title="toggle sidebar">☰</button>
      <span className="brand">TBCTL</span>
      <span className="chip">{dashboard?.name ?? "no dashboard"}</span>
      {status?.device.sim && <span className="chip warn"><span className="dot" />SIM</span>}
      <span className="spacer" />
      <span className={`chip ${deviceOk ? "ok" : "bad"}`}>
        <span className="dot" />T7 {status?.device.ip ?? "—"}
      </span>
      <span className={`chip ${hbFresh ? "ok" : "bad"}`}>
        <span className="dot" />{wsConnected ? "LINK" : "NO LINK"}
      </span>
      <span className={`chip state-${seqState}`}>SEQ {seqState}</span>
      <span className={`chip ${recording ? "rec" : ""}`}>
        <span className="dot" />{recording ? "REC" : "IDLE"}
      </span>
      <button
        className={`btn ${status?.control_enabled ? "danger-active" : ""}`}
        title="gates manual valves and ARM"
        onClick={() =>
          setControlMode(!(status?.control_enabled ?? false)).then(() =>
            fetchStatus().then((s) => useStore.getState().setStatus(s)),
          )
        }
      >
        {status?.control_enabled ? "CONTROL ON" : "control off"}
      </button>
      {editMode && dashboard && (
        <button
          className="btn"
          onClick={() =>
            saveDashboard(dashboard.name, dashboard)
              .then(() => setEditMode(false))
              .catch((e) => alert(String(e)))
          }
        >
          save
        </button>
      )}
      <button
        className={`btn ${editMode ? "active" : ""}`}
        onClick={() => setEditMode(!editMode)}
      >
        {editMode ? "done" : "edit"}
      </button>
    </header>
  );
}
