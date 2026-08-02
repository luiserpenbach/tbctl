import { useCallback, useEffect, useState } from "react";
import {
  abortSequence,
  armSequence,
  fetchSequences,
  fireSequence,
  loadSequence,
  resetSequence,
  type CompileReport,
  type SequenceInfo,
} from "../../api";
import { useStore } from "../../store";

type Pending = "arm" | "fire" | null;

export function SequencePanel() {
  const seq = useStore((s) => s.seqStatus);
  const status = useStore((s) => s.status);
  const [sequences, setSequences] = useState<SequenceInfo[]>([]);
  const [selected, setSelected] = useState("");
  const [report, setReport] = useState<CompileReport | null>(null);
  const [pending, setPending] = useState<Pending>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    fetchSequences()
      .then((r) => {
        setSequences(r.sequences);
        const loaded = r.sequences.find((s) => s.loaded);
        if (loaded) setSelected(loaded.name);
        else if (r.sequences.length) setSelected(r.sequences[0].name);
      })
      .catch(() => {});
  }, []);

  const doAbort = useCallback(() => {
    abortSequence().catch((e) => setErr(String(e)));
    setPending(null);
  }, []);

  // Esc-Esc -> ABORT (global while this panel is mounted)
  useEffect(() => {
    let lastEsc = 0;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        const now = Date.now();
        if (now - lastEsc < 600) doAbort();
        lastEsc = now;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [doAbort]);

  const state = seq?.state ?? "—";
  const controlEnabled = status?.control_enabled ?? false;
  const run = async (fn: () => Promise<unknown>) => {
    setErr("");
    try {
      await fn();
    } catch (e) {
      setErr(String(e).replace(/^Error: /, ""));
    }
  };

  const doLoad = () =>
    run(async () => {
      setReport(null);
      setReport((await loadSequence(selected)) as CompileReport);
    });

  const progress =
    state === "RUNNING" && report ? Math.min(1, (seq?.elapsed_ms ?? 0) / report.duration_ms) : null;

  return (
    <div className="seq-panel">
      <div className="seq-row">
        <select
          className="seq-select"
          value={selected}
          onChange={(e) => {
            setSelected(e.target.value);
            setReport(null);
            setPending(null);
          }}
        >
          {sequences.map((s) => (
            <option key={s.name} value={s.name}>
              {s.name} ({s.n_steps} steps)
            </option>
          ))}
        </select>
        <button className="btn" onClick={doLoad} disabled={!selected || state === "RUNNING"}>
          LOAD
        </button>
        {status?.checklist && (
          <span
            className={`chip ${status.checklist.checked === status.checklist.total ? "ok" : "warn"}`}
            title={`checklist ${status.checklist.name}`}
          >
            <span className="dot" />
            ✓ {status.checklist.checked}/{status.checklist.total}
          </span>
        )}
        {pending !== "arm" ? (
          <button
            className="btn"
            disabled={state !== "LOADED" || !controlEnabled}
            title={!controlEnabled ? "enable control mode first" : ""}
            onClick={() => setPending("arm")}
          >
            ARM
          </button>
        ) : (
          <button className="btn confirm" onClick={() => run(async () => armSequence(selected)).then(() => setPending(null))}>
            CONFIRM ARM {selected}?
          </button>
        )}
        {pending !== "fire" ? (
          <button className="btn" disabled={state !== "ARMED"} onClick={() => setPending("fire")}>
            FIRE
          </button>
        ) : (
          <button className="btn confirm" onClick={() => run(async () => fireSequence(selected)).then(() => setPending(null))}>
            CONFIRM FIRE {selected}?
          </button>
        )}
        {["DONE", "ABORTED", "FAULT", "LOADED"].includes(state) && (
          <button className="btn" onClick={() => run(resetSequence)}>
            RESET
          </button>
        )}
      </div>

      {report && (
        <div className="seq-report">
          <span className="chip ok">
            <span className="dot" />
            CRC 0x{report.crc16.toString(16).toUpperCase().padStart(4, "0")}{" "}
            {report.crc_match ? "✓ verified" : "✗ MISMATCH"}
          </span>
          <span className="chip">
            {report.n_steps} steps / {(report.duration_ms / 1000).toFixed(2)} s
          </span>
          <span className="chip">abort {report.n_abort_steps} steps</span>
          {report.redlines.map((r) => (
            <span key={r.tag} className="chip" title={`persist ${r.persist_ms} ms`}>
              RL {r.tag}
              {r.min !== null ? ` ≥${r.min}` : ""}
              {r.max !== null ? ` ≤${r.max}` : ""}
            </span>
          ))}
          {report.warnings.map((w, i) => (
            <span key={i} className="chip warn">
              <span className="dot" />
              {w}
            </span>
          ))}
        </div>
      )}

      <div className="seq-progress-row">
        <span className={`chip state-${state}`} style={{ fontSize: 13 }}>
          {state}
          {seq && seq.error !== "NONE" ? ` — ${seq.error}` : ""}
        </span>
        {(state === "RUNNING" || state === "ABORTING") && (
          <span className="chip">
            step {seq?.cur_step ?? 0} · {((seq?.elapsed_ms ?? 0) / 1000).toFixed(2)} s
          </span>
        )}
      </div>
      {progress !== null && (
        <div className="progress-outer">
          <div className="progress-inner" style={{ width: `${progress * 100}%` }} />
        </div>
      )}
      {err && <div style={{ color: "var(--red)", fontSize: 11, padding: "0 8px" }}>{err}</div>}

      <button className="abort-btn" onClick={doAbort} title="also: press Esc twice">
        ABORT
      </button>
    </div>
  );
}
