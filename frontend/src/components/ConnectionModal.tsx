import { useEffect, useState } from "react";
import { post } from "../api";

interface ConnInfo {
  mode: "sim" | "usb" | "ethernet";
  ip: string;
  connected: boolean;
  sim: boolean;
  last_error: string | null;
  stream: { running: boolean; scan_rate_hz: number; sample_count: number };
  device: { serial?: number; firmware?: number; temp_degc?: number; error?: string };
  interpreter: { version?: number; expected?: number; ok?: boolean; error?: string };
  watchdog_enabled: boolean | null;
}

export function ConnectionModal({ onClose }: { onClose: () => void }) {
  const [info, setInfo] = useState<ConnInfo | null>(null);
  const [ip, setIp] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  const refresh = () =>
    fetch("/api/connection")
      .then((r) => r.json())
      .then((i: ConnInfo) => {
        setInfo(i);
        if (!ip) setIp(i.ip);
      })
      .catch((e) => setMsg(String(e)));

  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 2000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doSwitch = async (mode: "sim" | "usb" | "ethernet") => {
    setBusy(true);
    setMsg(`switching to ${mode}…`);
    try {
      await post("/api/connection", { mode, ip: mode === "ethernet" ? ip : null });
      setMsg(`switched to ${mode}`);
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const doDeploy = async () => {
    setBusy(true);
    setMsg("deploying sequencer.lua…");
    try {
      const r = (await post("/api/connection/deploy-lua")) as { interp_version: number };
      setMsg(`deployed, interp_version ${r.interp_version}, watchdog armed`);
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const row = (k: string, v: React.ReactNode) => (
    <div className="modal-row" style={{ justifyContent: "space-between" }}>
      <span style={{ color: "var(--text-dim)" }}>{k}</span>
      <span style={{ fontFamily: "var(--mono)" }}>{v}</span>
    </div>
  );

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">connection</div>
        {info && (
          <>
            {row("mode", info.mode.toUpperCase())}
            {row(
              "device",
              info.device.error
                ? `unreachable: ${info.device.error.slice(0, 60)}`
                : `T7 #${info.device.serial} fw ${info.device.firmware} · ${info.device.temp_degc}°C`,
            )}
            {row(
              "interpreter",
              info.interpreter.error
                ? "unknown"
                : info.interpreter.ok
                  ? `v${info.interpreter.version} ✓`
                  : `v${info.interpreter.version} (expect v${info.interpreter.expected}) ✗`,
            )}
            {row("watchdog", info.watchdog_enabled === null ? "?" : info.watchdog_enabled ? "armed" : "OFF")}
            {row(
              "stream",
              `${info.stream.running ? "running" : "stopped"} @ ${info.stream.scan_rate_hz} Hz · ${info.stream.sample_count} samples`,
            )}
            <hr style={{ border: "none", borderTop: "1px solid var(--border)", width: "100%" }} />
            <div className="modal-row">
              <button
                className={`btn ${info.mode === "sim" ? "active" : ""}`}
                disabled={busy}
                onClick={() => doSwitch("sim")}
              >
                SIM
              </button>
              <button
                className={`btn ${info.mode === "usb" ? "active" : ""}`}
                disabled={busy}
                onClick={() => doSwitch("usb")}
              >
                USB
              </button>
              <button
                className={`btn ${info.mode === "ethernet" ? "active" : ""}`}
                disabled={busy}
                onClick={() => doSwitch("ethernet")}
              >
                ETHERNET
              </button>
              <input
                className="modal-input"
                style={{ width: 120 }}
                value={ip}
                onChange={(e) => setIp(e.target.value)}
                placeholder="device ip"
              />
            </div>
            <div className="modal-row">
              <button className="btn" disabled={busy || info.sim} onClick={doDeploy}>
                deploy sequencer.lua + arm watchdog
              </button>
            </div>
            <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
              switching requires: sequencer IDLE, not recording, control mode off
            </div>
          </>
        )}
        {msg && <div style={{ fontSize: 11, color: "var(--amber)", whiteSpace: "pre-wrap" }}>{msg}</div>}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>
            close
          </button>
        </div>
      </div>
    </div>
  );
}
