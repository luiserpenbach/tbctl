import { useEffect, useMemo, useState } from "react";
import { post } from "../api";

// Channel & calibration editor (roadmap phase 11). YAML stays the source of
// truth: this edits a local copy of /api/config/channels and POSTs the whole
// document back; the node validates, backs up, writes and hot-applies.

type Cal =
  | { kind: "linear"; gain: number; offset: number }
  | { kind: "polynomial"; coeffs: number[] }
  | { kind: "table"; x: number[]; y: number[] };

interface Chan {
  tag: string;
  name?: string;
  hw?: string;
  kind: "analog_in" | "thermocouple_k" | "valve" | "igniter" | "derived";
  unit?: string;
  range?: number;
  negative_ch?: string | null;
  cal?: Cal;
  rate_group?: string;
  fail_safe?: string;
  open_state?: string;
  on_state?: string;
  max_on_ms?: number;
  expr?: string;
}

interface Config {
  device: { ip: string; connection: string; watchdog_timeout_s: number; record_outputs?: boolean };
  rate_groups: Record<string, { rate_hz: number }>;
  channels: Chan[];
}

function evalCal(cal: Cal | undefined, v: number): number {
  if (!cal) return v;
  if (cal.kind === "linear") return v * cal.gain + cal.offset;
  if (cal.kind === "polynomial") return cal.coeffs.reduce((acc, c, i) => acc + c * v ** i, 0);
  // table: linear interp
  const { x, y } = cal;
  if (v <= x[0]) return y[0];
  if (v >= x[x.length - 1]) return y[y.length - 1];
  for (let i = 1; i < x.length; i++) {
    if (v <= x[i]) {
      const f = (v - x[i - 1]) / (x[i] - x[i - 1]);
      return y[i - 1] + f * (y[i] - y[i - 1]);
    }
  }
  return y[y.length - 1];
}

function CalPreview({ cal, range, raw }: { cal: Cal | undefined; range: number; raw: number | null }) {
  const pts = useMemo(() => {
    const n = 60;
    const out: [number, number][] = [];
    for (let i = 0; i <= n; i++) {
      const v = (i / n) * range;
      out.push([v, evalCal(cal, v)]);
    }
    return out;
  }, [cal, range]);
  const ys = pts.map((p) => p[1]);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const W = 260;
  const H = 90;
  const sx = (v: number) => (v / range) * (W - 8) + 4;
  const sy = (e: number) => H - 4 - ((e - yMin) / (yMax - yMin || 1)) * (H - 8);
  const line = pts.map(([v, e]) => `${sx(v).toFixed(1)},${sy(e).toFixed(1)}`).join(" ");
  return (
    <div>
      <svg width={W} height={H} style={{ background: "var(--bg)", borderRadius: 4 }}>
        <polyline points={line} fill="none" stroke="var(--accent)" strokeWidth="1.5" />
        {raw !== null && (
          <circle cx={sx(raw)} cy={sy(evalCal(cal, raw))} r="3.5" fill="var(--amber)" />
        )}
      </svg>
      <div style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
        0…{range} V → {yMin.toFixed(1)}…{yMax.toFixed(1)} EU
        {raw !== null && (
          <span style={{ color: "var(--amber)" }}>
            {" "}· now: {raw.toFixed(4)} V → {evalCal(cal, raw).toFixed(3)} EU
          </span>
        )}
      </div>
    </div>
  );
}

function ChannelForm({
  chan,
  raw,
  onSave,
  onCancel,
}: {
  chan: Chan;
  raw: number | null;
  onSave: (c: Chan) => void;
  onCancel: () => void;
}) {
  const [c, setC] = useState<Chan>(JSON.parse(JSON.stringify(chan)));
  const upd = (patch: Partial<Chan>) => setC({ ...c, ...patch });
  const isAnalog = c.kind === "analog_in";
  const isOutput = c.kind === "valve" || c.kind === "igniter";

  const tare = (target = 0) => {
    if (raw === null || !c.cal) return;
    if (c.cal.kind === "linear") {
      upd({ cal: { ...c.cal, offset: +(target - c.cal.gain * raw).toFixed(6) } });
    } else if (c.cal.kind === "polynomial") {
      const rest = c.cal.coeffs.reduce((a, k, i) => (i === 0 ? a : a + k * raw ** i), 0);
      const coeffs = [...c.cal.coeffs];
      coeffs[0] = +(target - rest).toFixed(6);
      upd({ cal: { ...c.cal, coeffs } });
    }
  };

  const inp = (v: string | number | undefined, cb: (s: string) => void, w = 90) => (
    <input className="modal-input" style={{ width: w, flex: "none" }} value={v ?? ""} onChange={(e) => cb(e.target.value)} />
  );

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{chan.tag ? `Edit ${chan.tag}` : "New channel"}</div>
        <div className="modal-row">
          <span style={{ width: 70 }}>tag</span>
          {inp(c.tag, (s) => upd({ tag: s }), 180)}
        </div>
        <div className="modal-row" title="P&ID / display name, e.g. PT-CC-01 — the channel name in hopper-test-lab exports">
          <span style={{ width: 70 }}>name</span>
          {inp(c.name, (s) => upd({ name: s }), 180)}
        </div>
        <div className="modal-row">
          <span style={{ width: 70 }}>kind</span>
          <select
            className="seq-select"
            value={c.kind}
            onChange={(e) => {
              const kind = e.target.value as Chan["kind"];
              const base: Chan = { tag: c.tag, name: c.name, hw: c.hw, kind };
              if (kind === "analog_in")
                Object.assign(base, { unit: "bar", range: 10.0, cal: { kind: "linear", gain: 1, offset: 0 }, rate_group: "default" });
              if (kind === "thermocouple_k") Object.assign(base, { unit: "degC", rate_group: "default" });
              if (kind === "valve") Object.assign(base, { fail_safe: "closed", open_state: "high" });
              if (kind === "igniter") Object.assign(base, { fail_safe: "off", on_state: "high", max_on_ms: 2000 });
              if (kind === "derived") {
                delete base.hw;
                Object.assign(base, { unit: "", expr: "" });
              }
              setC(base);
            }}
          >
            {["analog_in", "thermocouple_k", "valve", "igniter", "derived"].map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
          {c.kind !== "derived" && (
            <>
              <span style={{ width: 30 }}>pin</span>
              {inp(c.hw, (s) => upd({ hw: s.toUpperCase() }), 80)}
            </>
          )}
        </div>
        {c.kind === "derived" && (
          <>
            <div className="modal-row">
              <span style={{ width: 70 }}>unit</span>
              {inp(c.unit, (s) => upd({ unit: s }), 70)}
            </div>
            <div className="modal-row">
              <span style={{ width: 70 }}>expr</span>
              {inp(c.expr, (s) => upd({ expr: s }), 300)}
            </div>
            <div style={{ fontSize: 11.5, color: "var(--text-faint)" }}>
              arithmetic over physical channel tags, e.g.{" "}
              <code>pressure.ox_manifold - pressure.chamber</code> · functions: abs sqrt log
              log10 exp min max
            </div>
          </>
        )}
        {!isOutput && c.kind !== "derived" && (
          <div className="modal-row">
            <span style={{ width: 70 }}>unit</span>
            {inp(c.unit, (s) => upd({ unit: s }), 70)}
            <span>rate group</span>
            {inp(c.rate_group, (s) => upd({ rate_group: s }), 80)}
          </div>
        )}
        {(isAnalog || c.kind === "thermocouple_k") && (
          <div
            className="modal-row"
            title="written to the T7 before every stream start; neg = negative input of a differential pair (AIN0/AIN1 …, Mux80 AIN48/AIN56 …), empty = single-ended"
          >
            <span style={{ width: 70 }}>range</span>
            <select
              className="seq-select"
              style={{ flex: "none", minWidth: 0, width: 90 }}
              value={c.range ?? (isAnalog ? 10 : 0.1)}
              onChange={(e) => upd({ range: parseFloat(e.target.value) })}
            >
              {[10, 1, 0.1, 0.01].map((r) => (
                <option key={r} value={r}>
                  ±{r} V
                </option>
              ))}
            </select>
            <span>neg</span>
            {inp(c.negative_ch ?? "", (s) => upd({ negative_ch: s.trim() ? s.trim().toUpperCase() : null }), 80)}
            <span style={{ textTransform: "none", letterSpacing: 0 }}>
              {c.negative_ch ? "differential" : "single-ended"}
            </span>
          </div>
        )}
        {isAnalog && c.cal && (
          <>
            <div className="modal-row">
              <span style={{ width: 70 }}>cal</span>
              <select
                className="seq-select"
                value={c.cal.kind}
                onChange={(e) => {
                  const kind = e.target.value;
                  if (kind === "linear") upd({ cal: { kind: "linear", gain: 1, offset: 0 } });
                  else if (kind === "polynomial") upd({ cal: { kind: "polynomial", coeffs: [0, 1] } });
                  else upd({ cal: { kind: "table", x: [0, 10], y: [0, 100] } });
                }}
              >
                <option>linear</option>
                <option>polynomial</option>
                <option>table</option>
              </select>
              {c.cal.kind !== "table" && (
                <button className="btn" title="zero the reading at the current raw input" onClick={() => tare(0)} disabled={raw === null}>
                  tare→0
                </button>
              )}
            </div>
            {c.cal.kind === "linear" && (
              <div className="modal-row">
                <span style={{ width: 70 }}>gain / offset</span>
                {inp(c.cal.gain, (s) => upd({ cal: { kind: "linear", gain: parseFloat(s) || 0, offset: (c.cal as { offset: number }).offset } }))}
                {inp(c.cal.offset, (s) => upd({ cal: { kind: "linear", gain: (c.cal as { gain: number }).gain, offset: parseFloat(s) || 0 } }))}
              </div>
            )}
            {c.cal.kind === "polynomial" && (
              <div className="modal-row">
                <span style={{ width: 70 }}>coeffs c0..cN</span>
                {inp(c.cal.coeffs.join(", "), (s) =>
                  upd({ cal: { kind: "polynomial", coeffs: s.split(",").map((x) => parseFloat(x) || 0) } }), 220)}
              </div>
            )}
            {c.cal.kind === "table" && (
              <>
                <div className="modal-row">
                  <span style={{ width: 70 }}>x (V)</span>
                  {inp(c.cal.x.join(", "), (s) => upd({ cal: { ...(c.cal as { kind: "table"; x: number[]; y: number[] }), x: s.split(",").map((v) => parseFloat(v) || 0) } }), 220)}
                </div>
                <div className="modal-row">
                  <span style={{ width: 70 }}>y (EU)</span>
                  {inp(c.cal.y.join(", "), (s) => upd({ cal: { ...(c.cal as { kind: "table"; x: number[]; y: number[] }), y: s.split(",").map((v) => parseFloat(v) || 0) } }), 220)}
                </div>
              </>
            )}
            <CalPreview cal={c.cal} range={c.range ?? 10} raw={raw} />
          </>
        )}
        {c.kind === "valve" && (
          <div className="modal-row">
            <span style={{ width: 70 }}>fail safe</span>
            <select className="seq-select" value={c.fail_safe} onChange={(e) => upd({ fail_safe: e.target.value })}>
              <option>closed</option>
              <option>open</option>
            </select>
            <span>open state</span>
            <select className="seq-select" value={c.open_state} onChange={(e) => upd({ open_state: e.target.value })}>
              <option>high</option>
              <option>low</option>
            </select>
          </div>
        )}
        {c.kind === "igniter" && (
          <div className="modal-row">
            <span style={{ width: 70 }}>max on ms</span>
            {inp(c.max_on_ms, (s) => upd({ max_on_ms: parseInt(s) || 0 }), 80)}
            <span>on state</span>
            <select className="seq-select" value={c.on_state} onChange={(e) => upd({ on_state: e.target.value })}>
              <option>high</option>
              <option>low</option>
            </select>
          </div>
        )}
        <div className="modal-actions">
          <button className="btn" onClick={onCancel}>cancel</button>
          <button className="btn active" onClick={() => onSave(c)}>apply</button>
        </div>
      </div>
    </div>
  );
}

export function ChannelsEditor() {
  const [cfg, setCfg] = useState<Config | null>(null);
  const [dirty, setDirty] = useState(false);
  const [editing, setEditing] = useState<Chan | null>(null);
  const [raws, setRaws] = useState<Record<string, number>>({});
  // node-side engineering value; null when it cannot be converted (open TC)
  const [values, setValues] = useState<Record<string, number | null>>({});
  const [msg, setMsg] = useState("");

  useEffect(() => {
    fetch("/api/config/channels")
      .then((r) => r.json())
      .then((d) => setCfg(d.config))
      .catch((e) => setMsg(String(e)));
    const iv = setInterval(() => {
      fetch("/api/raw")
        .then((r) => r.json())
        .then((d) => {
          const m: Record<string, number> = {};
          const v: Record<string, number | null> = {};
          for (const row of d.channels) {
            if (row.raw !== null) m[row.tag] = row.raw;
            v[row.tag] = row.value;
          }
          setRaws(m);
          setValues(v);
        })
        .catch(() => {});
    }, 700);
    return () => clearInterval(iv);
  }, []);

  if (!cfg) return <div className="placeholder">loading…</div>;

  const applyChan = (orig: Chan | null, updated: Chan) => {
    const channels = orig
      ? cfg.channels.map((c) => (c === orig ? updated : c))
      : [...cfg.channels, updated];
    setCfg({ ...cfg, channels });
    setDirty(true);
    setEditing(null);
  };

  const save = async () => {
    setMsg("saving…");
    try {
      const r = (await post("/api/config/channels", cfg)) as { backup: string };
      setMsg(`saved & hot-applied (stream restarted). backup: ${r.backup ?? "n/a"}`);
      setDirty(false);
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    }
  };

  return (
    <div className="editor-page">
      <div className="editor-head">
        <span className="modal-title">Channels &amp; calibration</span>
        <button className="btn" onClick={() => setEditing({ tag: "", hw: "", kind: "analog_in", unit: "bar", range: 10, cal: { kind: "linear", gain: 1, offset: 0 }, rate_group: "default" })}>
          + Add channel
        </button>
        <button className="btn active" disabled={!dirty} onClick={save}>
          Save &amp; apply
        </button>
        {dirty && <span className="chip warn"><span className="dot" />unsaved changes</span>}
      </div>
      <div className="modal-row" style={{ padding: "0 2px" }}>
        <span>device ip</span>
        <input className="modal-input" style={{ width: 130, flex: "none" }} value={cfg.device.ip}
          onChange={(e) => { setCfg({ ...cfg, device: { ...cfg.device, ip: e.target.value } }); setDirty(true); }} />
        <span>connection</span>
        <select className="seq-select" style={{ flex: "none" }} value={cfg.device.connection}
          onChange={(e) => { setCfg({ ...cfg, device: { ...cfg.device, connection: e.target.value } }); setDirty(true); }}>
          <option>ethernet</option><option>usb</option><option>any</option>
        </select>
        <span>watchdog s</span>
        <input className="modal-input" style={{ width: 50, flex: "none" }} value={cfg.device.watchdog_timeout_s}
          onChange={(e) => { setCfg({ ...cfg, device: { ...cfg.device, watchdog_timeout_s: parseInt(e.target.value) || 5 } }); setDirty(true); }} />
      </div>
      <table className="value-table">
        <thead>
          <tr style={{ color: "var(--text-faint)", fontSize: 11.5, textTransform: "uppercase" }}>
            <td>tag</td><td>pin</td><td>kind</td><td>unit</td><td>cal</td><td>live</td><td></td>
          </tr>
        </thead>
        <tbody>
          {cfg.channels.map((c) => (
            <tr key={c.tag + c.hw}>
              <td>{c.tag}</td>
              <td style={{ color: "var(--text-dim)" }}>{c.hw ?? "calc"}</td>
              <td>{c.kind}</td>
              <td className="unit">{c.unit ?? c.fail_safe}</td>
              <td style={{ fontSize: 11, color: "var(--text-dim)" }}>
                {c.kind === "derived"
                  ? c.expr
                  : c.cal
                    ? c.cal.kind === "linear"
                      ? `×${c.cal.gain}+${c.cal.offset}`
                      : c.cal.kind
                    : "—"}
              </td>
              <td className="val">
                {c.kind === "thermocouple_k"
                  ? // no cal to preview: the node converts (cold junction, Type-K)
                    typeof values[c.tag] === "number"
                    ? (values[c.tag] as number).toFixed(2)
                    : raws[c.tag] !== undefined
                      ? "out of range"
                      : "—"
                  : raws[c.tag] !== undefined
                    ? `${evalCal(c.cal, raws[c.tag]).toFixed(2)}`
                    : "—"}
              </td>
              <td style={{ whiteSpace: "nowrap" }}>
                <button className="panel-btn" onClick={() => setEditing(c)}>⚙</button>
                <button className="panel-btn" onClick={() => { setCfg({ ...cfg, channels: cfg.channels.filter((x) => x !== c) }); setDirty(true); }}>✕</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {msg && <div style={{ fontSize: 11, color: "var(--amber)", padding: 6, whiteSpace: "pre-wrap" }}>{msg}</div>}
      {editing && (
        <ChannelForm
          chan={editing}
          raw={raws[editing.tag] ?? null}
          onSave={(u) => applyChan(cfg.channels.includes(editing) ? editing : null, u)}
          onCancel={() => setEditing(null)}
        />
      )}
    </div>
  );
}
