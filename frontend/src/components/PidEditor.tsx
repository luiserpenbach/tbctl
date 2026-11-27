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
import {
  bgOf,
  clampBg,
  clampSize,
  elSize,
  followEl,
  PidStage,
  type PidBg,
  type PidEdit,
} from "./pid/PidStage";

// Visual P&ID editor (roadmap phase 13b): choose/upload a background SVG,
// place valve/sensor/label elements, bind them to channels, save to
// configs/pid/<name>.yaml. Editing is keyboard-first: multi-select, copy /
// paste / duplicate, rotate, resize, nudge, align, undo / redo, zoom and pan
// (the shortcut list is rendered under the stage).

type ElPatch = Record<string, unknown>;
interface View {
  z: number; // zoom factor, 1 = fit
  x: number; // pan, fraction of the viewport (<= 0)
  y: number;
}

const FIT: View = { z: 1, x: 0, y: 0 };
const Z_MAX = 8;
const HISTORY = 200;
const clamp01 = (v: number) => +Math.min(1, Math.max(0, v)).toFixed(4);
const normDeg = (d: number) => ((d % 360) + 360) % 360;

// v_ox_vent -> v_ox_vent_2 -> v_ox_vent_3; v1 -> v2
function freeId(id: string, taken: Set<string>): string {
  const m = /^(.*?)(\d+)$/.exec(id);
  const stem = m ? m[1] : `${id}_`;
  let n = m ? parseInt(m[2]) + 1 : 2;
  while (taken.has(`${stem}${n}`)) n++;
  return `${stem}${n}`;
}

const SHORTCUTS: [string, string][] = [
  ["click / shift-click / drag box", "select"],
  ["drag", "move (shift: one axis, ctrl: no snapping)"],
  ["alt-drag, ctrl+D", "duplicate"],
  ["ctrl+C / X / V", "copy / cut / paste at cursor"],
  ["R / shift+R", "rotate 90°"],
  ["[ ]", "rotate 15°"],
  ["+ −, corner handle", "resize"],
  ["arrows (shift: ×5)", "nudge"],
  ["V / S / T", "add valve / sensor / text at cursor"],
  ["del", "delete"],
  ["ctrl+Z / ctrl+Y", "undo / redo"],
  ["G", "grid snap"],
  ["B", "background mode: drag moves it, corner handle or + − resizes it"],
  ["wheel, space-drag, 0", "zoom, pan, fit"],
  ["ctrl+S", "save"],
];

export function PidEditor() {
  const channels = useStore((s) => s.channels);
  const outputs = useMemo(
    () => channels.filter((c) => c.kind === "valve" || c.kind === "igniter"),
    [channels],
  );
  const inputs = useMemo(() => channels.filter(isReadable), [channels]);

  const [list, setList] = useState<PidInfo[]>([]);
  const [assets, setAssets] = useState<string[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [cfg, setCfgState] = useState<PidConfigT | null>(null);
  const [sel, setSelState] = useState<string[]>([]); // element ids, first = anchor
  const [dirty, setDirty] = useState(false);
  const [msg, setMsg] = useState("");
  const [saveAs, setSaveAs] = useState("");
  const [snap, setSnap] = useState(false);
  const [view, setView] = useState<View>(FIT);
  const [panning, setPanning] = useState(false);
  const [showKeys, setShowKeys] = useState(true);
  const [bgMode, setBgMode] = useState(false);
  const [bgFollow, setBgFollow] = useState(true);
  const fileRef = useRef<HTMLInputElement>(null);
  const viewRef = useRef<HTMLDivElement>(null);

  // refs mirror the state so key handlers and gestures always see the latest
  const cfgRef = useRef<PidConfigT | null>(null);
  const selRef = useRef<string[]>([]);
  const past = useRef<PidConfigT[]>([]);
  const future = useRef<PidConfigT[]>([]);
  const lastTag = useRef<{ tag: string; t: number } | null>(null);
  const clipboard = useRef<PidElement[]>([]);
  const pastes = useRef(0);
  const cursor = useRef<[number, number] | null>(null);
  const space = useRef(false);
  const pan = useRef<{ x: number; y: number; v: View } | null>(null);

  const setSel = (ids: string[]) => {
    selRef.current = ids;
    setSelState(ids);
  };
  const checkpoint = () => {
    if (!cfgRef.current) return;
    past.current.push(cfgRef.current);
    if (past.current.length > HISTORY) past.current.shift();
    future.current = [];
    lastTag.current = null;
  };
  // every edit goes through here; edits sharing a tag in quick succession
  // (typing in a field, holding an arrow key) collapse into one undo step
  const commit = (fn: (c: PidConfigT) => PidConfigT, tag?: string | false) => {
    const cur = cfgRef.current;
    if (!cur) return;
    if (tag !== false) {
      const now = Date.now();
      const lt = lastTag.current;
      const merge = tag !== undefined && lt?.tag === tag && now - lt.t < 1000;
      if (!merge) checkpoint();
      lastTag.current = tag !== undefined ? { tag, t: now } : null;
    }
    const next = fn(cur);
    cfgRef.current = next;
    setCfgState(next);
    setDirty(true);
  };
  const load = (c: PidConfigT | null, isDirty: boolean) => {
    cfgRef.current = c;
    setCfgState(c);
    past.current = [];
    future.current = [];
    lastTag.current = null;
    setSel([]);
    setDirty(isDirty);
    setView(FIT);
    setMsg("");
  };
  const stepHistory = (from: PidConfigT[], to: PidConfigT[]) => {
    const cur = cfgRef.current;
    const prev = from.pop();
    if (!cur || !prev) return;
    to.push(cur);
    lastTag.current = null;
    cfgRef.current = prev;
    setCfgState(prev);
    setDirty(true);
    const ids = new Set(prev.elements.map((e) => e.id));
    setSel(selRef.current.filter((i) => ids.has(i)));
  };
  const undo = () => stepHistory(past.current, future.current);
  const redo = () => stepHistory(future.current, past.current);

  const refresh = () => {
    fetchPids()
      .then((r) => setList(r.pids))
      .catch(() => {});
    fetchPidAssets()
      .then((r) => setAssets(r.assets))
      .catch(() => {});
  };
  useEffect(refresh, []);

  const okToDiscard = () => !dirty || window.confirm("Discard unsaved P&ID changes?");

  const select = async (name: string) => {
    if (!okToDiscard()) return;
    try {
      const r = await fetchPid(name);
      setSelected(name);
      load(r.config, false);
    } catch (e) {
      setMsg(String(e));
    }
  };

  const upd = (patch: Partial<PidConfigT>, tag?: string) => commit((c) => ({ ...c, ...patch }), tag);
  const mapEls = (
    ids: Iterable<string>,
    fn: (e: PidElement) => ElPatch | null,
    tag?: string | false,
  ) => {
    const set = new Set(ids);
    commit(
      (c) => ({
        ...c,
        elements: c.elements.map((e) => {
          const p = set.has(e.id) ? fn(e) : null;
          return p ? ({ ...e, ...p } as PidElement) : e;
        }),
      }),
      tag,
    );
  };
  const updEl = (id: string, patch: ElPatch) =>
    mapEls([id], () => patch, `el:${id}:${Object.keys(patch).join()}`);

  const addElement = (type: PidElement["type"], at: [number, number] | null = null) => {
    const c = cfgRef.current;
    if (!c) return;
    const ids = new Set(c.elements.map((e) => e.id));
    let n = 1;
    while (ids.has(`${type[0]}${n}`)) n++;
    const id = `${type[0]}${n}`;
    const base = { id, x: at?.[0] ?? 0.5, y: at?.[1] ?? 0.5 };
    // follow what is already on the sheet: same size as the last element of
    // this type, and the first channel that is not placed yet
    const like = [...c.elements].reverse().find((e) => e.type === type);
    const used = new Set(c.elements.map((e) => ("channel" in e ? e.channel : "")));
    const free = (pool: { tag: string }[]) => (pool.find((p) => !used.has(p.tag)) ?? pool[0])?.tag ?? "";
    let el: PidElement;
    if (type === "valve") {
      el = { ...base, type, channel: free(outputs), size: like ? elSize(like) : 0.05, rotation: 0 };
    } else if (type === "sensor") {
      el = { ...base, type, channel: free(inputs), decimals: 1 };
      if (like?.type === "sensor" && like.size != null) el.size = like.size;
    } else {
      el = { ...base, type, text: "LABEL", size: like ? elSize(like) : 0.022 };
    }
    commit((cur) => ({ ...cur, elements: [...cur.elements, el] }));
    setSel([id]);
  };

  // copies of `els`, shifted and with fresh ids; selects them
  const insertCopies = (els: PidElement[], dx: number, dy: number): string[] => {
    const c = cfgRef.current;
    if (!c || els.length === 0) return [];
    const taken = new Set(c.elements.map((e) => e.id));
    const copies = els.map((e) => {
      const id = freeId(e.id, taken);
      taken.add(id);
      return { ...e, id, x: clamp01(e.x + dx), y: clamp01(e.y + dy) } as PidElement;
    });
    commit((cur) => ({ ...cur, elements: [...cur.elements, ...copies] }));
    const ids = copies.map((e) => e.id);
    setSel(ids);
    return ids;
  };
  const selectedEls = () => {
    const set = new Set(selRef.current);
    return cfgRef.current?.elements.filter((e) => set.has(e.id)) ?? [];
  };
  const duplicate = () => insertCopies(selectedEls(), 0.02, 0.02);
  const copy = () => {
    const els = selectedEls();
    if (els.length) {
      clipboard.current = els;
      pastes.current = 0;
    }
  };
  const paste = () => {
    const els = clipboard.current;
    if (!els.length) return;
    pastes.current++;
    let dx = 0.02 * pastes.current;
    let dy = dx;
    if (cursor.current) {
      // drop the group centred on the pointer
      dx = cursor.current[0] - els.reduce((s, e) => s + e.x, 0) / els.length;
      dy = cursor.current[1] - els.reduce((s, e) => s + e.y, 0) / els.length;
    }
    insertCopies(els, dx, dy);
  };
  const remove = () => {
    const set = new Set(selRef.current);
    if (!set.size) return;
    commit((c) => ({ ...c, elements: c.elements.filter((e) => !set.has(e.id)) }));
    setSel([]);
  };
  const rotate = (deg: number) =>
    mapEls(
      selRef.current,
      (e) => (e.type === "sensor" ? null : { rotation: normDeg((e.rotation ?? 0) + deg) }),
      "rotate",
    );
  // place the background; with "elements follow" they stay on the drawing
  const applyBg = (
    to: PidBg,
    tag?: string | false,
    els?: Record<string, { x: number; y: number; size?: number }>,
  ) => {
    const next = clampBg(to);
    commit((c) => {
      const from = bgOf(c);
      return {
        ...c,
        background_scale: next.scale,
        background_x: next.x,
        background_y: next.y,
        elements: c.elements.map((e) => {
          const p = els ? els[e.id] : bgFollow ? followEl(e, from, next) : null;
          return p ? ({ ...e, ...p } as PidElement) : e;
        }),
      };
    }, tag);
  };
  // resize about the middle of the sheet
  const scaleBg = (f: number) => {
    const b = bgOf(cfgRef.current!);
    const k = clampBg({ ...b, scale: b.scale * f }).scale / b.scale;
    applyBg({ scale: b.scale * k, x: 0.5 - (0.5 - b.x) * k, y: 0.5 - (0.5 - b.y) * k }, "bg");
  };
  const moveBg = (dx: number, dy: number) => {
    const b = bgOf(cfgRef.current!);
    applyBg({ ...b, x: b.x + dx, y: b.y + dy }, "bg");
  };
  const scale = (f: number) =>
    bgMode ? scaleBg(f) :
    mapEls(selRef.current, (e) => ({ size: clampSize(e.type, elSize(e) * f) }), "scale");
  const nudge = (dx: number, dy: number) =>
    bgMode ? moveBg(dx, dy) :
    mapEls(selRef.current, (e) => ({ x: clamp01(e.x + dx), y: clamp01(e.y + dy) }), "nudge");
  // elements are centre-anchored, so aligning is just sharing one coordinate
  const align = (axis: "x" | "y") => {
    const anchor = selectedEls().find((e) => e.id === selRef.current[0]);
    if (anchor) mapEls(selRef.current, () => ({ [axis]: anchor[axis] }));
  };
  const distribute = (axis: "x" | "y") => {
    const els = [...selectedEls()].sort((a, b) => a[axis] - b[axis]);
    if (els.length < 3) return;
    const lo = els[0][axis];
    const step = (els[els.length - 1][axis] - lo) / (els.length - 1);
    const pos = new Map(els.map((e, i) => [e.id, +(lo + i * step).toFixed(4)]));
    mapEls(selRef.current, (e) => ({ [axis]: pos.get(e.id) }));
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
    const c = cfgRef.current;
    if (!c || !name) return;
    setMsg("saving…");
    try {
      const body = { ...c, name };
      const r = await savePid(name, body);
      setMsg(`saved ${name}.yaml (backup: ${r.backup ? "yes" : "none"})`);
      // edits made while the request was in flight stay unsaved
      if (cfgRef.current === c) {
        cfgRef.current = body;
        setCfgState(body);
        setDirty(false);
      }
      setSelected(name);
      setSaveAs("");
      refresh();
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    }
  };
  const isNew = cfg !== null && selected === null;
  const save = () => {
    const c = cfgRef.current;
    if (c) doSave(isNew ? c.name : (selected ?? c.name));
  };

  // ---- zoom / pan -------------------------------------------------------
  const clampView = (v: View): View => {
    const z = Math.min(Z_MAX, Math.max(1, v.z));
    return { z, x: Math.min(0, Math.max(1 - z, v.x)), y: Math.min(0, Math.max(1 - z, v.y)) };
  };
  const hasCfg = cfg !== null;
  useEffect(() => {
    const el = viewRef.current;
    if (!el) return;
    const onWheel = (ev: WheelEvent) => {
      ev.preventDefault();
      const r = el.getBoundingClientRect();
      const mx = (ev.clientX - r.left) / r.width;
      const my = (ev.clientY - r.top) / r.height;
      setView((v) => {
        const z = Math.min(Z_MAX, Math.max(1, v.z * Math.exp(-ev.deltaY * 0.0015)));
        const k = z / v.z;
        // keep the point under the pointer where it is
        return clampView({ z, x: mx - (mx - v.x) * k, y: my - (my - v.y) * k });
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [hasCfg]);

  // ---- keyboard ---------------------------------------------------------
  const keys = useRef<(ev: KeyboardEvent) => void>(() => {});
  keys.current = (ev) => {
    if (!cfgRef.current) return;
    const mod = ev.ctrlKey || ev.metaKey;
    const k = ev.key.length === 1 ? ev.key.toLowerCase() : ev.key;
    if (mod && k === "s") {
      ev.preventDefault();
      if (dirty) save();
      return;
    }
    const t = ev.target as HTMLElement | null;
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA")) {
      if (ev.key === "Escape") t.blur();
      return;
    }
    const done = () => ev.preventDefault();
    if (mod) {
      if (ev.altKey) return;
      if (k === "z") return done(), ev.shiftKey ? redo() : undo();
      if (k === "y") return done(), redo();
      if (k === "a") return done(), setSel(cfgRef.current.elements.map((e) => e.id));
      if (k === "d") return done(), void duplicate();
      if (k === "v") return done(), paste();
      // leave ctrl+C / ctrl+X alone while page text is selected
      if (window.getSelection()?.toString().trim()) return;
      if (k === "c") return done(), copy();
      if (k === "x") return done(), copy(), remove();
      return;
    }
    if (ev.altKey) return;
    const step = ev.shiftKey ? 0.01 : 0.002;
    switch (k) {
      case " ":
        space.current = true;
        return done();
      case "Escape":
        // not consumed: Esc Esc stays the abort gesture
        return bgMode ? setBgMode(false) : setSel([]);
      case "Delete":
      case "Backspace":
        return done(), remove();
      case "ArrowLeft":
        return done(), nudge(-step, 0);
      case "ArrowRight":
        return done(), nudge(step, 0);
      case "ArrowUp":
        return done(), nudge(0, -step);
      case "ArrowDown":
        return done(), nudge(0, step);
      case "r":
        return rotate(ev.shiftKey ? -90 : 90);
      case "]":
        return rotate(15);
      case "[":
        return rotate(-15);
      case "+":
      case "=":
        return scale(1.1);
      case "-":
      case "_":
        return scale(1 / 1.1);
      case "b":
        return setBgMode(!bgMode && !!cfgRef.current.background);
      case "g":
        return setSnap((s) => !s);
      case "0":
        return setView(FIT);
      case "v":
        return addElement("valve", cursor.current);
      case "s":
        return addElement("sensor", cursor.current);
      case "t":
        return addElement("label", cursor.current);
    }
  };
  useEffect(() => {
    const down = (ev: KeyboardEvent) => keys.current(ev);
    const upKey = (ev: KeyboardEvent) => {
      if (ev.key === " ") space.current = false;
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", upKey);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", upKey);
    };
  }, []);
  // closing the tab with unsaved work asks first
  useEffect(() => {
    if (!dirty) return;
    const warn = (ev: BeforeUnloadEvent) => ev.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const selSet = useMemo(() => new Set(sel), [sel]);
  const edit: PidEdit = {
    selected: selSet,
    snap,
    cursor,
    onSelect: setSel,
    checkpoint,
    patch: (patches) => mapEls(Object.keys(patches), (e) => patches[e.id], false),
    bgMode: bgMode && !!cfg?.background,
    bgFollow,
    patchBg: (to, els) => applyBg(to, false, els),
    duplicate: (ids) => {
      const set = new Set(ids);
      const els = cfgRef.current?.elements.filter((e) => set.has(e.id)) ?? [];
      // keep the caller's order
      els.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      return insertCopies(els, 0, 0);
    },
  };

  const one = sel.length === 1 ? (cfg?.elements.find((e) => e.id === sel[0]) ?? null) : null;
  const num = (v: string, fallback: number) => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : fallback;
  };
  const numInput = (
    value: number,
    onChange: (v: number) => void,
    opts: { step?: string; width?: number; min?: string; max?: string } = {},
  ) => (
    <input
      className="modal-input"
      style={{ width: opts.width ?? 78, flex: "none" }}
      type="number"
      step={opts.step}
      min={opts.min}
      max={opts.max}
      value={value}
      onChange={(e) => onChange(num(e.target.value, value))}
    />
  );

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
            if (!okToDiscard()) return;
            setSelected(null);
            load({ name: "new_pid", background: null, elements: [] }, true);
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
              onChange={(e) => upd({ name: e.target.value }, "name")}
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
            <button className="btn" title="undo (ctrl+Z)" disabled={!past.current.length} onClick={undo}>
              undo
            </button>
            <button className="btn" title="redo (ctrl+Y)" disabled={!future.current.length} onClick={redo}>
              redo
            </button>
            <button
              className={`btn ${snap ? "active" : ""}`}
              title="snap moves to a grid (G)"
              onClick={() => setSnap(!snap)}
            >
              grid
            </button>
            <button
              className={`btn ${bgMode ? "active" : ""}`}
              title="move / resize the background SVG (B)"
              disabled={!cfg.background}
              onClick={() => setBgMode(!bgMode)}
            >
              background
            </button>
            <button className="btn" title="reset zoom (0)" onClick={() => setView(FIT)}>
              {Math.round(view.z * 100)}%
            </button>
            <button className="btn" title="add valve at the pointer (V)" onClick={() => addElement("valve")}>
              + valve
            </button>
            <button className="btn" title="add sensor at the pointer (S)" onClick={() => addElement("sensor")}>
              + sensor
            </button>
            <button className="btn" title="add text at the pointer (T)" onClick={() => addElement("label")}>
              + label
            </button>
          </div>

          <div
            ref={viewRef}
            className={`pid-edit-stage ${panning ? "panning" : ""}`}
            onMouseDown={(ev) => {
              if (ev.button === 1) ev.preventDefault(); // no middle-click autoscroll
            }}
            onPointerDownCapture={(ev) => {
              if (ev.button !== 1 && !(ev.button === 0 && space.current)) return;
              ev.stopPropagation();
              ev.preventDefault();
              ev.currentTarget.setPointerCapture(ev.pointerId);
              pan.current = { x: ev.clientX, y: ev.clientY, v: view };
              setPanning(true);
            }}
            onPointerMove={(ev) => {
              const p = pan.current;
              if (!p) return;
              const r = ev.currentTarget.getBoundingClientRect();
              setView(
                clampView({
                  z: p.v.z,
                  x: p.v.x + (ev.clientX - p.x) / r.width,
                  y: p.v.y + (ev.clientY - p.y) / r.height,
                }),
              );
            }}
            onPointerUp={() => {
              pan.current = null;
              setPanning(false);
            }}
            onPointerCancel={() => {
              pan.current = null;
              setPanning(false);
            }}
          >
            <div
              style={{
                position: "absolute",
                left: `${view.x * 100}%`,
                top: `${view.y * 100}%`,
                width: `${view.z * 100}%`,
                height: `${view.z * 100}%`,
              }}
            >
              <PidStage
                cfg={cfg}
                valveKind={(tag) =>
                  channels.find((c) => c.tag === tag)?.kind === "igniter" ? "igniter" : "valve"
                }
                liveValve={() => false}
                liveValue={() => null}
                edit={edit}
              />
            </div>
          </div>

          {bgMode && cfg.background && (
            <div className="modal-row pid-inspector" style={{ flexWrap: "wrap" }}>
              <span className="chip">background</span>
              <span>scale</span>
              {numInput(bgOf(cfg).scale, (v) => scaleBg(v / bgOf(cfg).scale), { step: "0.05" })}
              <span>x</span>
              {numInput(bgOf(cfg).x, (v) => applyBg({ ...bgOf(cfg), x: v }, "bg"), { step: "0.01" })}
              <span>y</span>
              {numInput(bgOf(cfg).y, (v) => applyBg({ ...bgOf(cfg), y: v }, "bg"), { step: "0.01" })}
              <label style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input
                  type="checkbox"
                  checked={bgFollow}
                  onChange={(e) => setBgFollow(e.target.checked)}
                />
                elements follow
              </label>
              <button className="btn" onClick={() => applyBg({ scale: 1, x: 0, y: 0 })}>
                reset
              </button>
              <span style={{ color: "var(--subtle)" }}>
                drag to move, corner handle or + − to resize, arrows to nudge
              </span>
              <span className="spacer" />
              <button className="btn" title="Esc" onClick={() => setBgMode(false)}>
                done
              </button>
            </div>
          )}

          <div className="modal-row pid-inspector" style={{ flexWrap: "wrap" }}>
            {sel.length === 0 && (
              <span style={{ color: "var(--subtle)" }}>
                {cfg.elements.length} elements — click one to edit it, drag a box to select several
              </span>
            )}
            {sel.length > 1 && (
              <>
                <span className="chip">{sel.length} selected</span>
                <button className="btn" title="same height as the first selected" onClick={() => align("y")}>
                  align row
                </button>
                <button className="btn" title="same x as the first selected" onClick={() => align("x")}>
                  align column
                </button>
                <button className="btn" disabled={sel.length < 3} onClick={() => distribute("x")}>
                  space out ↔
                </button>
                <button className="btn" disabled={sel.length < 3} onClick={() => distribute("y")}>
                  space out ↕
                </button>
                <button className="btn" title="R" onClick={() => rotate(90)}>
                  rotate
                </button>
                <button className="btn" title="−" onClick={() => scale(1 / 1.1)}>
                  smaller
                </button>
                <button className="btn" title="+" onClick={() => scale(1.1)}>
                  larger
                </button>
              </>
            )}
            {one && (
              <>
                <span className="chip">{one.type}</span>
                <span>id</span>
                <input
                  className="modal-input"
                  style={{ width: 110, flex: "none" }}
                  value={one.id}
                  onChange={(e) => {
                    const id = e.target.value;
                    // one tag across the rename, so typing an id is one undo step
                    mapEls([one.id], () => ({ id }), "rename");
                    setSel([id]);
                  }}
                />
                {one.type !== "label" && (
                  <>
                    <span>channel</span>
                    <select
                      className="seq-select"
                      value={one.channel}
                      onChange={(e) => updEl(one.id, { channel: e.target.value })}
                    >
                      {(one.type === "valve" ? outputs : inputs).map((c) => (
                        <option key={c.tag} value={c.tag}>
                          {c.tag}
                        </option>
                      ))}
                    </select>
                  </>
                )}
                {one.type === "label" && (
                  <>
                    <span>text</span>
                    <input
                      className="modal-input"
                      style={{ width: 160, flex: "none" }}
                      value={one.text}
                      onChange={(e) => updEl(one.id, { text: e.target.value })}
                    />
                  </>
                )}
                {one.type === "sensor" ? (
                  <>
                    <span>size</span>
                    <input
                      className="modal-input"
                      style={{ width: 78, flex: "none" }}
                      type="number"
                      step="0.002"
                      placeholder="auto"
                      title="font height as a fraction of the sheet width; empty = fixed 12 px"
                      value={one.size ?? ""}
                      onChange={(e) =>
                        updEl(one.id, {
                          size: e.target.value === "" ? undefined : clampSize("sensor", num(e.target.value, 0.012)),
                        })
                      }
                    />
                    <span>decimals</span>
                    {numInput(one.decimals ?? 1, (v) => updEl(one.id, { decimals: Math.round(v) }), {
                      width: 45,
                      min: "0",
                      max: "5",
                    })}
                    <span>warn min/max</span>
                    {(["warn_min", "warn_max"] as const).map((key) => (
                      <input
                        key={key}
                        className="modal-input"
                        style={{ width: 60, flex: "none" }}
                        type="number"
                        value={one[key] ?? ""}
                        onChange={(e) =>
                          updEl(one.id, {
                            [key]: e.target.value === "" ? undefined : num(e.target.value, 0),
                          })
                        }
                      />
                    ))}
                  </>
                ) : (
                  <>
                    <span>size</span>
                    {numInput(elSize(one), (v) => updEl(one.id, { size: clampSize(one.type, v) }), {
                      step: one.type === "valve" ? "0.005" : "0.002",
                    })}
                    <span>rotation</span>
                    {numInput(one.rotation ?? 0, (v) => updEl(one.id, { rotation: normDeg(v) }), {
                      step: "15",
                    })}
                  </>
                )}
                <span>x</span>
                {numInput(one.x, (v) => updEl(one.id, { x: clamp01(v) }), { step: "0.005", width: 84 })}
                <span>y</span>
                {numInput(one.y, (v) => updEl(one.id, { y: clamp01(v) }), { step: "0.005", width: 84 })}
              </>
            )}
            {sel.length > 0 && (
              <>
                <button className="btn" title="ctrl+D, or alt-drag" onClick={duplicate}>
                  duplicate
                </button>
                <button className="btn" title="del" onClick={remove}>
                  delete
                </button>
              </>
            )}
            <span className="spacer" />
            <button className="btn" onClick={() => setShowKeys(!showKeys)}>
              {showKeys ? "hide shortcuts" : "shortcuts"}
            </button>
          </div>

          {showKeys && (
            <div className="pid-keys">
              {SHORTCUTS.map(([k, what]) => (
                <span key={k}>
                  <kbd>{k}</kbd> {what}
                </span>
              ))}
            </div>
          )}

          <div className="editor-head">
            <button
              className="btn active"
              title="ctrl+S"
              disabled={!dirty || (isNew && !cfg.name)}
              onClick={save}
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
                    if (!selected || !window.confirm(`Delete P&ID ${selected}?`)) return;
                    deletePid(selected)
                      .then(() => {
                        load(null, false);
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
