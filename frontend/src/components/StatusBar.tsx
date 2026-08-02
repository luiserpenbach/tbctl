import { useEffect, useState } from "react";
import { fetchStatus, saveDashboard, setControlMode } from "../api";
import { useStore } from "../store";
import { ConnectionModal } from "./ConnectionModal";

export function StatusBar() {
  const status = useStore((s) => s.status);
  const seq = useStore((s) => s.seqStatus);
  const wsConnected = useStore((s) => s.wsConnected);
  const lastSeqAt = useStore((s) => s.lastSeqStatusAt);
  const editMode = useStore((s) => s.editMode);
  const setEditMode = useStore((s) => s.setEditMode);
  const toggleSidebar = useStore((s) => s.toggleSidebar);
  const dashboard = useStore((s) => s.dashboard);
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);

  const [connOpen, setConnOpen] = useState(false);
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
      <button
        className={`btn ${view === "dashboard" ? "active" : ""}`}
        onClick={() => setView("dashboard")}
      >
        Dashboard
      </button>
      <button
        className={`btn ${view === "channels" ? "active" : ""}`}
        onClick={() => setView("channels")}
      >
        Channels
      </button>
      <button
        className={`btn ${view === "sequences" ? "active" : ""}`}
        onClick={() => setView("sequences")}
      >
        Sequences
      </button>
      <button className={`btn ${view === "pid" ? "active" : ""}`} onClick={() => setView("pid")}>
        P&amp;ID
      </button>
      <span className="chip">{dashboard?.name ?? "no dashboard"}</span>
      {status?.device.sim && <span className="chip warn"><span className="dot" />SIM</span>}
      <span className="spacer" />
      <span
        className={`chip ${deviceOk ? "ok" : "bad"}`}
        style={{ cursor: "pointer" }}
        title="connection settings"
        onClick={() => setConnOpen(true)}
      >
        <span className="dot" />
        {(status?.device.mode ?? "T7").toUpperCase()} {status?.device.mode === "ethernet" ? status?.device.ip : ""}
      </span>
      {connOpen && <ConnectionModal onClose={() => setConnOpen(false)} />}
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
          setControlMode(!(status?.control_enabled ?? false))
            .then(() => fetchStatus().then((s) => useStore.getState().setStatus(s)))
            .catch((e) =>
              // M26: a silent failure here leaves the operator believing the
              // bench is (un)locked when it is not
              alert(`control mode toggle failed: ${String(e).replace(/^Error: /, "")}`),
            )
        }
      >
        {status?.control_enabled ? "Control on" : "Control off"}
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
          Save layout
        </button>
      )}
      <button
        className={`btn ${editMode ? "active" : ""}`}
        onClick={() => setEditMode(!editMode)}
      >
        {editMode ? "Done" : "Edit"}
      </button>
    </header>
  );
}
