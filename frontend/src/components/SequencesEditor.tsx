import { useEffect, useMemo, useState } from "react";
import type { BoundCfg, SequenceConfig, SequenceInfo, TimelineData, ValidateOk } from "../api";
import { fetchSequenceConfig, fetchSequences, saveSequence, validateSequence } from "../api";
import { useStore } from "../store";
import { isReadable, type ChannelInfo } from "../types";

// Sequence editor (roadmap phase 12). YAML stays the source of truth: this
// edits a local copy of a sequence and POSTs the whole document back; the
// node validates + compiles, backs up, writes. The timeline preview renders
// the *compiled* table (carry-forward full-state words) — what you see is
// literally what fires.

const num = (s: string): number | undefined => (s === "" ? undefined : parseFloat(s) || 0);

function shortTag(tag: string): string {
  const i = tag.indexOf(".");
  return i >= 0 ? tag.slice(i + 1) : tag;
}

// ---------------------------------------------------------------- steps table

function StepsTable({
  what,
  steps,
  outputs,
  onChange,
}: {
  what: string;
  steps: SequenceConfig["steps"];
  outputs: ChannelInfo[];
  onChange: (steps: SequenceConfig["steps"]) => void;
}) {
  const setCell = (i: number, tag: string, v: string) => {
    const next = steps.map((s, j) => {
      if (j !== i) return s;
      const set = { ...s.set };
      if (v === "") delete set[tag];
      else set[tag] = v;
      return { ...s, set };
    });
    onChange(next);
  };
  const setT = (i: number, v: number) =>
    onChange(steps.map((s, j) => (j === i ? { ...s, t: v } : s)));
  const insertAfter = (i: number) => {
    const t = steps.length ? +(steps[Math.max(i, 0)].t + 0.1).toFixed(3) : 0;
    const next = [...steps];
    next.splice(i + 1, 0, { t, set: {} });
    onChange(next);
  };
  const del = (i: number) => onChange(steps.filter((_, j) => j !== i));
  const move = (i: number, d: number) => {
    const j = i + d;
    if (j < 0 || j >= steps.length) return;
    const next = [...steps];
    [next[i], next[j]] = [next[j], next[i]];
    onChange(next);
  };

  return (
    <table className="value-table seq-steps">
      <thead>
        <tr style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase" }}>
          <td>#</td>
          <td>t [s]</td>
          {outputs.map((o) => (
            <td key={o.tag} title={o.tag}>
              {shortTag(o.tag)}
            </td>
          ))}
          <td />
        </tr>
      </thead>
      <tbody>
        {steps.map((s, i) => (
          <tr key={i}>
            <td style={{ color: "var(--text-faint)" }}>{i}</td>
            <td>
              <input
                className="modal-input seq-t"
                type="number"
                step="0.01"
                min="0"
                value={s.t}
                onChange={(e) => setT(i, parseFloat(e.target.value) || 0)}
              />
            </td>
            {outputs.map((o) => {
              const opts = o.kind === "igniter" ? ["on", "off"] : ["open", "close"];
              const v = s.set[o.tag] ?? "";
              return (
                <td key={o.tag}>
                  <select
                    className={`seq-select seq-cell ${v ? `cell-${v}` : "cell-inherit"}`}
                    value={v}
                    onChange={(e) => setCell(i, o.tag, e.target.value)}
                  >
                    <option value="">—</option>
                    {opts.map((x) => (
                      <option key={x} value={x}>
                        {x}
                      </option>
                    ))}
                  </select>
                </td>
              );
            })}
            <td style={{ whiteSpace: "nowrap" }}>
              <button className="panel-btn" title="insert row below" onClick={() => insertAfter(i)}>
                +
              </button>
              <button className="panel-btn" title="move up" onClick={() => move(i, -1)}>
                ↑
              </button>
              <button className="panel-btn" title="move down" onClick={() => move(i, 1)}>
                ↓
              </button>
              <button className="panel-btn" title="delete row" onClick={() => del(i)}>
                ✕
              </button>
            </td>
          </tr>
        ))}
        {steps.length === 0 && (
          <tr>
            <td colSpan={outputs.length + 3}>
              <button className="btn" onClick={() => insertAfter(-1)}>
                + first {what} step
              </button>
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}

// -------------------------------------------- preconditions / redlines table

function BoundsTable({
  rows,
  inputs,
  persist,
  onChange,
}: {
  rows: BoundCfg[];
  inputs: ChannelInfo[];
  persist: boolean; // redlines have persist_ms, preconditions don't
  onChange: (rows: BoundCfg[]) => void;
}) {
  const upd = (i: number, patch: Partial<BoundCfg>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <table className="value-table">
      <thead>
        <tr style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase" }}>
          <td>channel</td>
          <td>min</td>
          <td>max</td>
          {persist && <td>persist ms</td>}
          <td />
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i}>
            <td>
              <select
                className="seq-select"
                value={r.tag}
                onChange={(e) => upd(i, { tag: e.target.value })}
              >
                {!inputs.some((c) => c.tag === r.tag) && <option value={r.tag}>{r.tag}</option>}
                {inputs.map((c) => (
                  <option key={c.tag} value={c.tag}>
                    {c.tag}
                  </option>
                ))}
              </select>
            </td>
            <td>
              <input
                className="modal-input seq-t"
                type="number"
                value={r.min ?? ""}
                onChange={(e) => upd(i, { min: num(e.target.value) })}
              />
            </td>
            <td>
              <input
                className="modal-input seq-t"
                type="number"
                value={r.max ?? ""}
                onChange={(e) => upd(i, { max: num(e.target.value) })}
              />
            </td>
            {persist && (
              <td>
                <input
                  className="modal-input seq-t"
                  type="number"
                  min="0"
                  value={r.persist_ms ?? 50}
                  onChange={(e) => upd(i, { persist_ms: parseInt(e.target.value) || 0 })}
                />
              </td>
            )}
            <td>
              <button className="panel-btn" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                ✕
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

// ------------------------------------------------------------ timeline lanes

function niceTick(endMs: number): number {
  for (const t of [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 30000, 60000])
    if (endMs / t <= 10) return t;
  return 120000;
}

function TimelineLanes({
  title,
  tl,
  steps,
  endMs,
}: {
  title: string;
  tl: TimelineData;
  steps: { t_ms: number; word: number }[];
  endMs: number;
}) {
  const labelW = 110;
  const plotW = 620;
  const laneH = 14;
  const gap = 7;
  const axisH = 18;
  const H = tl.outputs.length * (laneH + gap) + axisH;
  const end = Math.max(endMs, 1);
  const sx = (t: number) => labelW + (t / end) * plotW;
  const tick = niceTick(end);
  const ticks: number[] = [];
  for (let t = 0; t <= end; t += tick) ticks.push(t);

  return (
    <div>
      <div className="seq-tl-title">{title}</div>
      <svg
        width={labelW + plotW + 30}
        height={H}
        style={{ background: "var(--bg)", borderRadius: 4 }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={sx(t)} y1={0} x2={sx(t)} y2={H - axisH + 4} stroke="var(--border)" />
            <text x={sx(t)} y={H - 4} fill="var(--text-faint)" fontSize="9" textAnchor="middle">
              {t >= 1000 ? `${t / 1000}s` : `${t}ms`}
            </text>
          </g>
        ))}
        {tl.outputs.map((o, row) => {
          const y = row * (laneH + gap) + 2;
          // carry-forward: active intervals for this bit, starting from safe_word
          const segs: [number, number][] = [];
          let active = ((tl.safe_word >> o.bit) & 1) === o.active_level;
          let start = 0;
          for (const s of steps) {
            const act = ((s.word >> o.bit) & 1) === o.active_level;
            if (act !== active) {
              if (active) segs.push([start, s.t_ms]);
              else start = s.t_ms;
              active = act;
            }
          }
          if (active) segs.push([start, end]);
          const color = o.kind === "igniter" ? "var(--amber)" : "var(--accent)";
          return (
            <g key={o.tag}>
              <text x={labelW - 8} y={y + laneH - 3} fill="var(--text-dim)" fontSize="10" textAnchor="end">
                {shortTag(o.tag)}
              </text>
              <line
                x1={labelW}
                y1={y + laneH - 1}
                x2={labelW + plotW}
                y2={y + laneH - 1}
                stroke="var(--border)"
              />
              {segs.map(([t0, t1], k) => (
                <rect
                  key={k}
                  x={sx(t0)}
                  y={y}
                  width={Math.max(sx(t1) - sx(t0), 1.5)}
                  height={laneH - 2}
                  fill={color}
                  opacity="0.75"
                  rx="1.5"
                />
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

// ------------------------------------------------------------------ the page

const freshSequence = (outputs: ChannelInfo[]): SequenceConfig => {
  const safeSet: Record<string, string> = {};
  for (const o of outputs)
    safeSet[o.tag] = o.kind === "igniter" ? "off" : o.fail_safe === "open" ? "open" : "close";
  return {
    name: "new_sequence",
    description: "",
    test_type: null,
    max_duration_s: 10,
    preconditions: [],
    steps: [{ t: 0, set: { ...safeSet } }],
    abort: [{ t: 0, set: { ...safeSet } }],
    allow_unsafe_end: false,
    redlines: [],
  };
};

export function SequencesEditor() {
  const channels = useStore((s) => s.channels);
  const outputs = useMemo(
    () =>
      channels
        .filter((c) => c.kind === "valve" || c.kind === "igniter")
        .sort((a, b) => (a.bit ?? 0) - (b.bit ?? 0)),
    [channels],
  );
  const inputs = useMemo(
    () => channels.filter(isReadable),
    [channels],
  );

  const [list, setList] = useState<SequenceInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [cfg, setCfg] = useState<SequenceConfig | null>(null);
  const [editable, setEditable] = useState(true);
  const [dirty, setDirty] = useState(false);
  const [result, setResult] = useState<ValidateOk | null>(null);
  const [errors, setErrors] = useState<{ loc: string; msg: string }[]>([]);
  const [msg, setMsg] = useState("");
  const [saveAs, setSaveAs] = useState("");

  const refreshList = () =>
    fetchSequences()
      .then((r) => setList(r.sequences))
      .catch(() => {});
  useEffect(() => {
    refreshList();
  }, []);

  const select = async (name: string) => {
    try {
      const r = await fetchSequenceConfig(name);
      setSelected(name);
      setCfg(r.config);
      setEditable(r.editable);
      setDirty(false);
      setMsg(r.editable ? "" : "read-only: this sequence is loaded on the device (reset to edit)");
    } catch (e) {
      setMsg(String(e));
    }
  };

  // debounced dry-run compile -> report + timeline, or per-field errors
  useEffect(() => {
    if (!cfg) return;
    const h = setTimeout(() => {
      validateSequence(cfg).then((r) => {
        if (r.ok) {
          setResult(r);
          setErrors([]);
        } else {
          setResult(null);
          setErrors(r.errors);
        }
      });
    }, 400);
    return () => clearTimeout(h);
  }, [cfg]);

  const upd = (patch: Partial<SequenceConfig>) => {
    if (!cfg) return;
    setCfg({ ...cfg, ...patch });
    setDirty(true);
  };

  const doSave = async (name: string) => {
    if (!cfg || !name) return;
    setMsg("saving…");
    try {
      const r = await saveSequence(name, cfg);
      setMsg(`saved ${name}.yaml (backup: ${r.backup ? "yes" : "none"})`);
      setDirty(false);
      setSelected(name);
      setCfg({ ...cfg, name });
      setSaveAs("");
      refreshList();
    } catch (e) {
      setMsg(String(e).replace(/^Error: /, ""));
    }
  };

  const isNew = cfg !== null && selected === null;

  return (
    <div className="editor-page seq-editor">
      <div className="editor-head">
        <span className="modal-title">sequences</span>
        {list.map((s) => (
          <button
            key={s.name}
            className={`btn ${selected === s.name ? "active" : ""}`}
            title={s.description}
            onClick={() => select(s.name)}
          >
            {s.name}
            {s.loaded ? " ●" : ""}
          </button>
        ))}
        <button
          className="btn"
          onClick={() => {
            setSelected(null);
            setCfg(freshSequence(outputs));
            setEditable(true);
            setDirty(true);
            setMsg("");
          }}
        >
          + new
        </button>
      </div>

      {!cfg && <div className="placeholder">select a sequence to edit, or create a new one</div>}

      {cfg && (
        <>
          <div className="modal-row" style={{ padding: "0 2px", flexWrap: "wrap" }}>
            <span>name</span>
            <input
              className="modal-input"
              style={{ width: 150, flex: "none" }}
              value={cfg.name}
              disabled={!isNew}
              onChange={(e) => upd({ name: e.target.value })}
            />
            <span>description</span>
            <input
              className="modal-input"
              style={{ width: 260, flex: "none" }}
              value={cfg.description}
              disabled={!editable}
              onChange={(e) => upd({ description: e.target.value })}
            />
            <span title="hopper-test-lab test type stamped on runs that fire this sequence">
              test type
            </span>
            <select
              className="seq-select"
              value={cfg.test_type ?? ""}
              disabled={!editable}
              onChange={(e) =>
                upd({ test_type: (e.target.value || null) as SequenceConfig["test_type"] })
              }
            >
              <option value="">— none —</option>
              <option value="engine">engine (hot fire)</option>
              <option value="igniter">igniter</option>
              <option value="cold_flow">cold flow</option>
            </select>
            <span>max duration s</span>
            <input
              className="modal-input"
              style={{ width: 60, flex: "none" }}
              type="number"
              min="0.1"
              value={cfg.max_duration_s}
              disabled={!editable}
              onChange={(e) => upd({ max_duration_s: parseFloat(e.target.value) || 0 })}
            />
            <label style={{ display: "flex", gap: 4, alignItems: "center", fontSize: 12 }}>
              <input
                type="checkbox"
                checked={cfg.allow_unsafe_end}
                disabled={!editable}
                onChange={(e) => upd({ allow_unsafe_end: e.target.checked })}
              />
              allow unsafe end
            </label>
          </div>

          <fieldset disabled={!editable} className="seq-fields">
            <div className="seq-section">steps</div>
            <StepsTable
              what="steps"
              steps={cfg.steps}
              outputs={outputs}
              onChange={(steps) => upd({ steps })}
            />

            <div className="seq-section">abort table</div>
            <StepsTable
              what="abort"
              steps={cfg.abort}
              outputs={outputs}
              onChange={(abort) => upd({ abort })}
            />

            <div className="seq-section">
              preconditions
              <button
                className="panel-btn"
                onClick={() =>
                  upd({
                    preconditions: [
                      ...cfg.preconditions,
                      { tag: inputs[0]?.tag ?? "", min: 0 },
                    ],
                  })
                }
              >
                +
              </button>
            </div>
            {cfg.preconditions.length > 0 && (
              <BoundsTable
                rows={cfg.preconditions}
                inputs={inputs}
                persist={false}
                onChange={(preconditions) => upd({ preconditions })}
              />
            )}

            <div className="seq-section">
              redlines (host auto-abort while armed/running)
              <button
                className="panel-btn"
                onClick={() =>
                  upd({
                    redlines: [
                      ...cfg.redlines,
                      { tag: inputs[0]?.tag ?? "", max: 0, persist_ms: 50 },
                    ],
                  })
                }
              >
                +
              </button>
            </div>
            {cfg.redlines.length > 0 && (
              <BoundsTable
                rows={cfg.redlines}
                inputs={inputs}
                persist={true}
                onChange={(redlines) => upd({ redlines })}
              />
            )}
          </fieldset>

          <div className="editor-head">
            <button
              className="btn active"
              disabled={!editable || !dirty || errors.length > 0 || (isNew && !cfg.name)}
              onClick={() => doSave(isNew ? cfg.name : (selected ?? cfg.name))}
            >
              save
            </button>
            {!isNew && (
              <>
                <input
                  className="modal-input"
                  style={{ width: 140, flex: "none" }}
                  placeholder="save-as name"
                  value={saveAs}
                  onChange={(e) => setSaveAs(e.target.value)}
                />
                <button className="btn" disabled={!saveAs || errors.length > 0} onClick={() => doSave(saveAs)}>
                  save as
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

          {errors.length > 0 && (
            <div className="seq-errors">
              {errors.map((e, i) => (
                <div key={i}>
                  <span style={{ color: "var(--text-faint)" }}>{e.loc}</span> {e.msg}
                </div>
              ))}
            </div>
          )}

          {result && (
            <>
              <div className="seq-report">
                compiled OK: {result.report.n_steps} steps / {result.report.n_abort_steps} abort ·{" "}
                {(result.timeline.duration_ms / 1000).toFixed(2)} s · CRC 0x
                {result.report.crc16.toString(16).toUpperCase().padStart(4, "0")} ·{" "}
                {result.report.size_bytes} B
                {result.report.warnings.map((w, i) => (
                  <div key={i} style={{ color: "var(--amber)" }}>
                    ⚠ {w}
                  </div>
                ))}
              </div>
              <TimelineLanes
                title="timeline preview (compiled main table)"
                tl={result.timeline}
                steps={result.timeline.steps}
                endMs={result.timeline.duration_ms}
              />
              <TimelineLanes
                title="abort timeline"
                tl={result.timeline}
                steps={result.timeline.abort}
                endMs={result.timeline.abort_duration_ms}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
