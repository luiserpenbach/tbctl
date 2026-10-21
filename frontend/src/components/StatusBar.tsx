import { useEffect, useState } from "react";
import { abortSequence, fetchStatus, saveDashboard, setControlMode } from "../api";
import { useStore } from "../store";
import { getThemePref, setThemePref, type ThemePref } from "../theme-pref";
import { ConnectionModal } from "./ConnectionModal";

type View = "dashboard" | "channels" | "sequences" | "pid";
const NAV: [View, string][] = [
  ["dashboard", "Dashboard"],
  ["channels", "Channels"],
  ["sequences", "Sequences"],
  ["pid", "P&ID"],
];

// annunciator colour per sequencer state
const SEQ_TONE: Record<string, string> = {
  LOADED: "info",
  ARMED: "warn",
  RUNNING: "ok",
  DONE: "ok",
  ABORTING: "bad",
  ABORTED: "bad",
  FAULT: "bad",
};

const pad = (n: number, w = 2) => String(Math.floor(n)).padStart(w, "0");
const hms = (s: number) => `${pad(s / 3600)}:${pad((s % 3600) / 60)}:${pad(s % 60)}`;
const mmss = (ms: number) => `${pad(ms / 60000)}:${pad((ms % 60000) / 1000)}.${pad((ms % 1000) / 10)}`;

function HopperMark() {
  // lander silhouette: body, legs, feet and a plume
  return (
    <svg className="brand-mark" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="7.5" y="3.5" width="9" height="8" rx="2.5" />
      <path d="M10 7.5h4" opacity="0.55" />
      <path d="M8.5 11.5 4.5 18.5M15.5 11.5l4 7M2.8 18.5h3.4M17.8 18.5h3.4M12 11.5v1.5" />
      <path d="M10.5 13.5h3L12 21z" fill="currentColor" stroke="none" opacity="0.55" />
    </svg>
  );
}

const THEME_ICONS: Record<ThemePref, React.ReactNode> = {
  light: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </svg>
  ),
  system: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="4" width="18" height="12" rx="2" />
      <path d="M8 20h8M12 16v4" />
    </svg>
  ),
  dark: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
    </svg>
  ),
};

function ThemeSwitch() {
  const [pref, setPref] = useState<ThemePref>(getThemePref);
  return (
    <div className="theme-switch" role="group" aria-label="theme">
      {(["light", "system", "dark"] as const).map((p) => (
        <button
          key={p}
          type="button"
          className={pref === p ? "active" : ""}
          aria-pressed={pref === p}
          title={p === "system" ? "Theme: follow the system" : `Theme: ${p}`}
          onClick={() => {
            setThemePref(p);
            setPref(p);
          }}
        >
          {THEME_ICONS[p]}
        </button>
      ))}
    </div>
  );
}

function MissionClock() {
  const seq = useStore((s) => s.seqStatus);
  const status = useStore((s) => s.status);
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const iv = setInterval(() => setNow(new Date()), 250);
    return () => clearInterval(iv);
  }, []);

  const utc = now.toISOString().slice(11, 19);
  const state = seq?.state ?? status?.sequencer.state ?? "";
  const rec = status?.recording;

  let cell = { cls: "", label: "Local", value: now.toLocaleTimeString(undefined, { hour12: false }) };
  if (state === "RUNNING") cell = { cls: "t-plus", label: "T+ fire", value: mmss(seq?.elapsed_ms ?? 0) };
  else if (state === "ABORTING") cell = { cls: "abort", label: "Abort", value: mmss(seq?.elapsed_ms ?? 0) };
  else if (state === "ARMED") cell = { cls: "armed", label: "Armed", value: "T− HOLD" };
  else if (rec?.active && rec.t_start)
    cell = { cls: "rec", label: "Rec", value: hms(Math.max(0, now.getTime() / 1000 - rec.t_start)) };

  return (
    <div className="mission-clock" title="mission clock">
      <div className="clock-cell utc">
        <span className="clock-label">UTC</span>
        <span className="clock-value">{utc}</span>
      </div>
      <div className={`clock-cell ${cell.cls || "local"}`}>
        <span className="clock-label">{cell.label}</span>
        <span className="clock-value">{cell.value}</span>
      </div>
    </div>
  );
}

function Ann({
  k,
  v,
  tone = "",
  onClick,
  title,
}: {
  k: string;
  v: string;
  tone?: string;
  onClick?: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      className={`ann ${tone} ${onClick ? "clickable" : ""}`}
      onClick={onClick}
      title={title}
      tabIndex={onClick ? 0 : -1}
    >
      <span className="led" />
      <span className="ann-key">{k}</span>
      <span className="ann-val">{v}</span>
    </button>
  );
}

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
  const control = status?.control_enabled ?? false;
  const mode = status?.device.mode ?? "t7";
  const deviceLabel = !deviceOk
    ? "Lost"
    : mode === "ethernet"
      ? (status?.device.ip ?? "Ethernet")
      : mode === "usb"
        ? "USB"
        : "Sim";

  const toggleControl = () =>
    setControlMode(!control).then(() => fetchStatus().then((s) => useStore.getState().setStatus(s)));

  return (
    <header className="statusbar">
      <button className="icon-btn" onClick={toggleSidebar} title="toggle sidebar" aria-label="toggle sidebar">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
          <rect x="3.5" y="4.5" width="17" height="15" rx="2" />
          <path d="M9 4.5v15" />
        </svg>
      </button>
      <div className="brand">
        <HopperMark />
        <div className="brand-text">
          <span className="brand-name">Hopper</span>
          <span className="brand-sub">Test &amp; mission control</span>
        </div>
      </div>
      <nav className="nav">
        {NAV.map(([v, label]) => (
          <button key={v} className={`nav-btn ${view === v ? "active" : ""}`} onClick={() => setView(v)}>
            {label}
          </button>
        ))}
      </nav>
      <span className="spacer" />
      <MissionClock />
      <div className="annunciators">
        <Ann
          k="T7"
          v={deviceLabel}
          tone={!deviceOk ? "bad" : status?.device.sim ? "warn" : "ok"}
          onClick={() => setConnOpen(true)}
          title={status?.device.sim ? "simulator: no hardware — connection settings" : "connection settings"}
        />
        <Ann k="Link" v={hbFresh ? "Up" : "Down"} tone={hbFresh ? "ok" : "bad"} />
        <Ann k="Seq" v={seqState} tone={SEQ_TONE[seqState] ?? ""} />
        <Ann k="Rec" v={recording ? "On" : "Off"} tone={recording ? "rec" : ""} />
        <Ann
          k="Control"
          v={control ? "Live" : "Safe"}
          tone={control ? "warn" : ""}
          onClick={toggleControl}
          title="gates manual valves and ARM — click to toggle"
        />
      </div>
      {(seqState === "ARMED" || seqState === "RUNNING") && (
        // always on screen while it matters, however the dashboard scrolls
        <button className="bar-abort" onClick={() => abortSequence().catch(() => {})} title="abort (also: Esc Esc)">
          Abort
        </button>
      )}
      {connOpen && <ConnectionModal onClose={() => setConnOpen(false)} />}
      <ThemeSwitch />
      {editMode && dashboard && (
        <button
          className="btn primary"
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
        className={`icon-btn ${editMode ? "active" : ""}`}
        onClick={() => setEditMode(!editMode)}
        title={editMode ? "done editing" : "edit layout"}
        aria-label="edit layout"
        style={editMode ? { color: "var(--accent)", borderColor: "var(--border)" } : undefined}
      >
        {editMode ? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="m5 12.5 4.5 4.5L19 7.5" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
            <rect x="3.5" y="3.5" width="7" height="7" rx="1.5" />
            <rect x="13.5" y="3.5" width="7" height="4" rx="1.5" />
            <rect x="13.5" y="10.5" width="7" height="10" rx="1.5" />
            <rect x="3.5" y="13.5" width="7" height="7" rx="1.5" />
          </svg>
        )}
      </button>
    </header>
  );
}
