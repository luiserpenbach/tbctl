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

  const state = seq?.state ?? "—";
  // re-read the list whenever the sequencer changes state, so a sequence
  // loaded from another client (or the API) shows up here as loaded
  useEffect(() => {
    fetchSequences()
      .then((r) => {
        setSequences(r.sequences);
        const loaded = r.sequences.find((s) => s.loaded);
        if (loaded) setSelected(loaded.name);
        else setSelected((cur) => cur || r.sequences[0]?.name || "");
      })
      .catch(() => {});
  }, [state]);

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

  const loadedName = sequences.find((s) => s.loaded)?.name ?? (report ? selected : "");
  const live = state === "RUNNING" || state === "ABORTING";

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
        <button className="btn" onClick={doLoad} disabled={!selected || live}>
          Load
        </button>
        {status?.checklist && (
          <span
            className={`chip ${status.checklist.checked === status.checklist.total ? "ok" : "warn"}`}
            title={`checklist ${status.checklist.name}`}
          >
            <span className="dot" />
            {status.checklist.checked}/{status.checklist.total}
          </span>
        )}
      </div>

      <div className={`seq-hero state-${state}`}>
        <span className="seq-state-word">{state}</span>
        {seq && seq.error !== "NONE" && <span className="chip bad"><span className="dot" />{seq.error}</span>}
        <div className="seq-meta">
          {live ? (
            <>
              <span className="k">step {seq?.cur_step ?? 0}</span>
              <span className="v">T+ {((seq?.elapsed_ms ?? 0) / 1000).toFixed(2)} s</span>
            </>
          ) : (
            <>
              <span className="k">
                {!loadedName ? "sequence" : state === "IDLE" ? "last loaded" : "loaded"}
              </span>
              <span className="v">{loadedName || "—"}</span>
            </>
          )}
        </div>
      </div>

      <div className="seq-actions">
        {pending !== "arm" ? (
          <button
            className="btn"
            disabled={state !== "LOADED" || !controlEnabled}
            title={!controlEnabled ? "enable control mode first" : ""}
            onClick={() => setPending("arm")}
          >
            Arm
          </button>
        ) : (
          <button className="btn confirm" onClick={() => run(async () => armSequence(selected)).then(() => setPending(null))}>
            Confirm arm
          </button>
        )}
        {pending !== "fire" ? (
          <button
            className={`btn ${state === "ARMED" ? "fire-ready" : ""}`}
            disabled={state !== "ARMED"}
            onClick={() => setPending("fire")}
          >
            Fire
          </button>
        ) : (
          <button className="btn confirm" onClick={() => run(async () => fireSequence(selected)).then(() => setPending(null))}>
            Confirm fire
          </button>
        )}
        <button
          className="btn"
          disabled={!["DONE", "ABORTED", "FAULT", "LOADED"].includes(state)}
          onClick={() => run(resetSequence)}
        >
          Reset
        </button>
      </div>

      <div className="seq-scroll">
      {report && (
        <div className="seq-report">
          <span className={`chip ${report.crc_match ? "ok" : "bad"}`}>
            <span className="dot" />
            CRC 0x{report.crc16.toString(16).toUpperCase().padStart(4, "0")}{" "}
            {report.crc_match ? "verified" : "MISMATCH"}
          </span>
          <span className="chip">
            {report.n_steps} steps · {(report.duration_ms / 1000).toFixed(2)} s
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

      {progress !== null && (
        <div className="progress-outer">
          <div className="progress-inner" style={{ width: `${progress * 100}%` }} />
        </div>
      )}
      {err && <div className="err-text">{err}</div>}
      </div>

      <button className="abort-btn" onClick={doAbort} title="also: press Esc twice">
        <span className="abort-key">
          ABORT
          <span className="abort-hint">ESC ESC</span>
        </span>
      </button>
    </div>
  );
}
