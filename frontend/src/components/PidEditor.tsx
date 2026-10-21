import { useEffect, useMemo, useRef, useState } from "react";
import {
  deletePid,
  fetchPid,
  fetchPidAssets,
  fetchPids,
  savePid,
  uploadPidAsset,
  type PidConfigT,
  type PidElement,
  type PidInfo,
} from "../api";
import { useStore } from "../store";
import { isReadable } from "../types";
import { PidStage } from "./pid/PidStage";

// Visual P&ID editor (roadmap phase 13b): choose/upload a background SVG,
// place valve/sensor/label elements from the palette, drag to position,
// bind to channels, save to configs/pid/<name>.yaml.

export function PidEditor() {
  const channels = useStore((s) => s.channels);
  const outputs = useMemo(
    () => channels.filter((c) => c.kind === "valve" || c.kind === "igniter"),
    [channels],
  );
  const inputs = useMemo(
    () => channels.filter(isReadable),
    [channels],
  );

  const [list, setList] = useState<PidInfo[]>([]);
  const [assets, setAssets] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [cfg, setCfg] = useState<PidConfigT | null>(null);
  const [selId, setSelId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [saveAs, setSaveAs] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = () => {
    fetchPids()
      .then((r) => setList(r.pids))
      .catch(() => {});
    fetchPidAssets()
      .then((r) => setAssets(r.assets))
      .catch(() => {});
  };
  useEffect(refresh, []);

  const select = async (name: string) => {
    try {
      const r = await fetchPid(name);
      setSelected(name);
      setCfg(r.config);
      setSelId(null);
      setDirty(false);
      setMsg("");
    } catch (e) {
      setMsg(String(e));
    }
  };

  const upd = (patch: Partial<PidConfigT>) => {
    if (!cfg) return;
    setCfg({ ...cfg, ...patch });
    setDirty(true);
  };
  const updEl = (id: string, patch: Record<string, unknown>) => {
    if (!cfg) return;
    upd({
      elements: cfg.elements.map((e) => (e.id === id ? ({ ...e, ...patch } as PidElement) : e)),
    });
  };

  const addElement = (type: PidElement["type"]) => {
    if (!cfg) return;
    let n = 1;
    const ids = new Set(cfg.elements.map((e) => e.id));
    while (ids.has(`${type[0]}${n}`)) n++;
    const id = `${type[0]}${n}`;
    const base = { id, x: 0.5, y: 0.5 };
    const el: PidElement =
      type === "valve"
        ? { ...base, type, channel: outputs[0]?.tag ?? "", size: 0.05, rotation: 0 }
        : type === "sensor"
          ? { ...base, type, channel: inputs[0]?.tag ?? "", decimals: 1 }
          : { ...base, type, text: "LABEL", size: 0.022 };
    upd({ elements: [...cfg.elements, el] });
    setSelId(id);
  };

  const uploadSvg = (file: File) => {
    const name = file.name.replace(/\.svg$/i, "").replace(/[^a-zA-Z0-9_-]/g, "_");
    file.text().then((svg) =>
      uploadPidAsset(name, svg)
        .then((r) => {
          setMsg(`uploaded ${r.asset}`);
          refresh();
          upd({ background: r.asset });
        })
        .catch((e) => setMsg(String(e).replace(/^Error: /, ""))),
    );
  };

  const doSave = async (name: string) => {
    if (!cfg || !name) return;
    setMsg("saving…");
    try {
      const r = await savePid(name, cfg);
      setMsg(`saved ${name}.yaml (backup: ${r.backup ? "yes" : "none"})`);
      setDirty(false);
      setSelected(name);
      setCfg({ ...cfg, name });
      setSaveAs("");
      refresh();
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    }
  };

  const sel = cfg?.elements.find((e) => e.id === selId) ?? null;
  const isNew = cfg !== null && selected === null;

  return (
    <div className="editor-page pid-editor">
      <div className="editor-head">
        <span className="modal-title">P&amp;ID editor</span>
        {list.map((p) => (
          <button
            key={p.name}
            className={`btn ${selected === p.name ? "active" : ""}`}
            onClick={() => select(p.name)}
          >
            {p.name}
          </button>
        ))}
        <button
          className="btn"
          onClick={() => {
            setSelected(null);
            setCfg({ name: "new_pid", background: null, elements: [] });
            setSelId(null);
            setDirty(true);
            setMsg("");
          }}
        >
          + new
        </button>
      </div>

      {!cfg && <div className="placeholder">select a P&amp;ID to edit, or create a new one</div>}

      {cfg && (
        <>
          <div className="modal-row" style={{ padding: "0 2px", flexWrap: "wrap" }}>
            <span>name</span>
            <input
              className="modal-input"
              style={{ width: 130, flex: "none" }}
              value={cfg.name}
              disabled={!isNew}
              onChange={(e) => upd({ name: e.target.value })}
            />
            <span>background</span>
            <select
              className="seq-select"
              value={cfg.background ?? ""}
              onChange={(e) => upd({ background: e.target.value || null })}
            >
              <option value="">none</option>
              {assets.map((a) => (
                <option key={a} value={a}>
                  {a}
                </option>
              ))}
            </select>
            <button className="btn" onClick={() => fileRef.current?.click()}>
              upload svg
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".svg"
              style={{ display: "none" }}
              onChange={(e) => {
                if (e.target.files?.[0]) uploadSvg(e.target.files[0]);
                e.target.value = "";
              }}
            />
            <span className="spacer" />
            <button className="btn" onClick={() => addElement("valve")}>
              + valve
            </button>
            <button className="btn" onClick={() => addElement("sensor")}>
              + sensor
            </button>
            <button className="btn" onClick={() => addElement("label")}>
              + label
            </button>
          </div>

          <div className="pid-edit-stage">
            <PidStage
              cfg={cfg}
              valveKind={(tag) =>
                channels.find((c) => c.tag === tag)?.kind === "igniter" ? "igniter" : "valve"
              }
              liveValve={() => false}
              liveValue={() => null}
              editing
              selectedId={selId}
              onElementClick={(el) => setSelId(el.id)}
              onElementDrag={(id, x, y) => {
                updEl(id, { x, y });
                setSelId(id);
              }}
            />
          </div>

          {sel && (
            <div className="modal-row pid-inspector" style={{ flexWrap: "wrap" }}>
              <span className="chip">{sel.type}</span>
              <span>id</span>
              <input
                className="modal-input"
                style={{ width: 90, flex: "none" }}
                value={sel.id}
                onChange={(e) => {
                  const id = e.target.value;
                  updEl(sel.id, { id });
                  setSelId(id);
                }}
              />
              {sel.type !== "label" && (
                <>
                  <span>channel</span>
                  <select
                    className="seq-select"
                    value={sel.channel}
                    onChange={(e) => updEl(sel.id, { channel: e.target.value })}
                  >
                    {(sel.type === "valve" ? outputs : inputs).map((c) => (
                      <option key={c.tag} value={c.tag}>
                        {c.tag}
                      </option>
                    ))}
                  </select>
                </>
              )}
              {sel.type === "valve" && (
                <>
                  <span>size</span>
                  <input
                    className="modal-input"
                    style={{ width: 60, flex: "none" }}
                    type="number"
                    step="0.005"
                    value={sel.size ?? 0.05}
                    onChange={(e) => updEl(sel.id, { size: parseFloat(e.target.value) || 0.05 })}
                  />
                  <span>rotation</span>
                  <input
                    className="modal-input"
                    style={{ width: 60, flex: "none" }}
                    type="number"
                    step="15"
                    value={sel.rotation ?? 0}
                    onChange={(e) => updEl(sel.id, { rotation: parseFloat(e.target.value) || 0 })}
                  />
                </>
              )}
              {sel.type === "sensor" && (
                <>
                  <span>decimals</span>
                  <input
                    className="modal-input"
                    style={{ width: 45, flex: "none" }}
                    type="number"
                    min="0"
                    max="5"
                    value={sel.decimals ?? 1}
                    onChange={(e) => updEl(sel.id, { decimals: parseInt(e.target.value) || 0 })}
                  />
                  <span>warn min/max</span>
                  <input
                    className="modal-input"
                    style={{ width: 60, flex: "none" }}
                    type="number"
                    value={sel.warn_min ?? ""}
                    onChange={(e) =>
                      updEl(sel.id, {
                        warn_min: e.target.value === "" ? undefined : parseFloat(e.target.value),
                      })
                    }
                  />
                  <input
                    className="modal-input"
                    style={{ width: 60, flex: "none" }}
                    type="number"
                    value={sel.warn_max ?? ""}
                    onChange={(e) =>
                      updEl(sel.id, {
                        warn_max: e.target.value === "" ? undefined : parseFloat(e.target.value),
                      })
                    }
                  />
                </>
              )}
              {sel.type === "label" && (
                <>
                  <span>text</span>
                  <input
                    className="modal-input"
                    style={{ width: 160, flex: "none" }}
                    value={sel.text}
                    onChange={(e) => updEl(sel.id, { text: e.target.value })}
                  />
                  <span>size</span>
                  <input
                    className="modal-input"
                    style={{ width: 60, flex: "none" }}
                    type="number"
                    step="0.002"
                    value={sel.size ?? 0.022}
                    onChange={(e) => updEl(sel.id, { size: parseFloat(e.target.value) || 0.022 })}
                  />
                </>
              )}
              <span>
                x {sel.x.toFixed(3)} y {sel.y.toFixed(3)}
              </span>
              <button
                className="btn"
                onClick={() => {
                  upd({ elements: cfg.elements.filter((e) => e.id !== sel.id) });
                  setSelId(null);
                }}
              >
                delete element
              </button>
            </div>
          )}

          <div className="editor-head">
            <button
              className="btn active"
              disabled={!dirty || (isNew && !cfg.name)}
              onClick={() => doSave(isNew ? cfg.name : (selected ?? cfg.name))}
            >
              save
            </button>
            {!isNew && (
              <>
                <input
                  className="modal-input"
                  style={{ width: 130, flex: "none" }}
                  placeholder="save-as name"
                  value={saveAs}
                  onChange={(e) => setSaveAs(e.target.value)}
                />
                <button className="btn" disabled={!saveAs} onClick={() => doSave(saveAs)}>
                  save as
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    if (!selected) return;
                    deletePid(selected)
                      .then(() => {
                        setCfg(null);
                        setSelected(null);
                        setMsg(`deleted ${selected}`);
                        refresh();
                      })
                      .catch((e) => setMsg(String(e)));
                  }}
                >
                  delete P&amp;ID
                </button>
              </>
            )}
            {dirty && (
              <span className="chip warn">
                <span className="dot" />
                unsaved changes
              </span>
            )}
            {msg && <span style={{ fontSize: 11, color: "var(--amber)" }}>{msg}</span>}
          </div>
        </>
      )}
    </div>
  );
}
