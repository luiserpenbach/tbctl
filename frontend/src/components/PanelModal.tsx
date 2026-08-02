import { useEffect, useState } from "react";
import { fetchPids } from "../api";
import { useStore } from "../store";
import type { PanelConfig } from "../types";
import { PANEL_TITLES } from "./DashboardGrid";

const PANEL_TYPES: PanelConfig["type"][] = [
  "timeseries",
  "value_table",
  "valve_panel",
  "sequence_panel",
  "recorder_panel",
  "raw_table",
  "sim_panel",
  "pid",
  "checklist_panel",
];

const NEEDS_CHANNELS = new Set(["timeseries", "value_table"]);

function ChannelPicker({
  selected,
  onChange,
  allowGlobs,
}: {
  selected: string[];
  onChange: (tags: string[]) => void;
  allowGlobs: boolean;
}) {
  const channels = useStore((s) => s.channels);
  const inputs = channels.filter((c) => c.kind === "analog_in" || c.kind === "thermocouple_k");
  const [glob, setGlob] = useState("");
  const toggle = (tag: string) =>
    onChange(selected.includes(tag) ? selected.filter((t) => t !== tag) : [...selected, tag]);
  return (
    <div className="chan-picker">
      {allowGlobs && (
        <div style={{ display: "flex", gap: 4, marginBottom: 6 }}>
          <input
            className="modal-input"
            placeholder="glob e.g. pressure.*"
            value={glob}
            onChange={(e) => setGlob(e.target.value)}
          />
          <button
            className="btn"
            onClick={() => {
              if (glob.trim()) {
                onChange([...selected, glob.trim()]);
                setGlob("");
              }
            }}
          >
            add
          </button>
        </div>
      )}
      <div className="chan-picker-list">
        {inputs.map((c) => (
          <label key={c.tag} className="chan-picker-row">
            <input
              type="checkbox"
              checked={selected.includes(c.tag)}
              onChange={() => toggle(c.tag)}
            />
            {c.tag} <span style={{ color: "var(--text-faint)" }}>{c.unit}</span>
          </label>
        ))}
      </div>
      {selected.filter((t) => t.includes("*")).length > 0 && (
        <div style={{ fontSize: 11, color: "var(--text-dim)" }}>
          globs:{" "}
          {selected
            .filter((t) => t.includes("*"))
            .map((g) => (
              <span key={g} className="chip" style={{ cursor: "pointer" }} onClick={() => toggle(g)}>
                {g} ×
              </span>
            ))}
        </div>
      )}
    </div>
  );
}

export function PanelModal({
  panel,
  onClose,
}: {
  panel: PanelConfig | null; // null = create new
  onClose: () => void;
}) {
  const dashboard = useStore((s) => s.dashboard);
  const addPanel = useStore((s) => s.addPanel);
  const updatePanel = useStore((s) => s.updatePanel);
  const [type, setType] = useState<PanelConfig["type"]>(panel?.type ?? "timeseries");
  const [channels, setChannels] = useState<string[]>(panel?.channels ?? []);
  const [yMode, setYMode] = useState<"auto" | "manual">(panel?.y_axis.mode ?? "auto");
  const [yMin, setYMin] = useState(String(panel?.y_axis.min ?? 0));
  const [yMax, setYMax] = useState(String(panel?.y_axis.max ?? 10));
  const [pidName, setPidName] = useState(panel?.pid ?? "");
  const [pidList, setPidList] = useState<string[]>([]);

  useEffect(() => {
    if (type === "pid")
      fetchPids()
        .then((r) => {
          setPidList(r.pids.map((p) => p.name));
          if (!pidName && r.pids[0]) setPidName(r.pids[0].name);
        })
        .catch(() => {});
  }, [type]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!dashboard) return null;

  const save = () => {
    const y_axis =
      yMode === "manual"
        ? { mode: "manual" as const, min: parseFloat(yMin), max: parseFloat(yMax) }
        : { mode: "auto" as const, min: null, max: null };
    const pid = type === "pid" ? pidName || null : null;
    if (panel) {
      updatePanel(panel.id, { channels, y_axis, type, pid });
    } else {
      let n = 1;
      const ids = new Set(dashboard.panels.map((p) => p.id));
      while (ids.has(`p${n}`)) n++;
      const bottom = Math.max(0, ...dashboard.panels.map((p) => p.y + p.h));
      addPanel({
        id: `p${n}`,
        type,
        x: 0,
        y: bottom,
        w: type === "timeseries" || type === "pid" ? 8 : 4,
        h: type === "timeseries" ? 7 : type === "pid" ? 9 : 5,
        channels,
        y_axis,
        pid,
      });
    }
    onClose();
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-title">{panel ? `configure ${panel.id}` : "add panel"}</div>
        {!panel && (
          <div className="modal-row">
            <span>type</span>
            <select
              className="seq-select"
              value={type}
              onChange={(e) => setType(e.target.value as PanelConfig["type"])}
            >
              {PANEL_TYPES.map((t) => (
                <option key={t} value={t}>
                  {PANEL_TITLES[t]}
                </option>
              ))}
            </select>
          </div>
        )}
        {NEEDS_CHANNELS.has(type) && (
          <ChannelPicker
            selected={channels}
            onChange={setChannels}
            allowGlobs={type === "value_table"}
          />
        )}
        {type === "pid" && (
          <div className="modal-row">
            <span>P&amp;ID</span>
            <select
              className="seq-select"
              value={pidName}
              onChange={(e) => setPidName(e.target.value)}
            >
              {pidList.length === 0 && <option value="">none available</option>}
              {pidList.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
        )}
        {type === "timeseries" && (
          <div className="modal-row">
            <span>y-axis</span>
            <select
              className="seq-select"
              value={yMode}
              onChange={(e) => setYMode(e.target.value as "auto" | "manual")}
            >
              <option value="auto">auto</option>
              <option value="manual">manual</option>
            </select>
            {yMode === "manual" && (
              <>
                <input className="modal-input" style={{ width: 70 }} value={yMin} onChange={(e) => setYMin(e.target.value)} />
                <input className="modal-input" style={{ width: 70 }} value={yMax} onChange={(e) => setYMax(e.target.value)} />
              </>
            )}
          </div>
        )}
        <div className="modal-actions">
          <button className="btn" onClick={onClose}>
            cancel
          </button>
          <button className="btn active" onClick={save}>
            {panel ? "apply" : "add"}
          </button>
        </div>
      </div>
    </div>
  );
}
