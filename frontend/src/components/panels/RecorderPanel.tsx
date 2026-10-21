import { useEffect, useState } from "react";
import { fetchRuns, postRunNote, startRun, stopRun } from "../../api";
import { useStore } from "../../store";

export function RecorderPanel() {
  const status = useStore((s) => s.status);
  const setRuns = useStore((s) => s.setRuns);
  const [name, setName] = useState("");
  const [operator, setOperator] = useState("");
  const [notes, setNotes] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [, setClock] = useState(0);
  const [note, setNote] = useState("");
  const [noteMsg, setNoteMsg] = useState("");

  useEffect(() => {
    const iv = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(iv);
  }, []);

  const rec = status?.recording;
  const elapsed =
    rec?.active && rec.t_start ? Math.max(0, (Date.now() / 1000 - rec.t_start) | 0) : 0;

  const doStart = async () => {
    setBusy(true);
    setErr("");
    try {
      await startRun(name || "run", operator || "unknown", notes);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const doStop = async () => {
    setBusy(true);
    setErr("");
    try {
      await stopRun();
      fetchRuns().then((r) => setRuns(r.runs));
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const addNote = () => {
    const text = note.trim();
    if (!text) return;
    postRunNote(text)
      .then(() => {
        setNote("");
        setNoteMsg(`noted @ ${new Date().toISOString().slice(11, 19)}: ${text.slice(0, 40)}`);
      })
      .catch((e) => setErr(String(e)));
  };

  return (
    <div className="rec-panel">
      {rec?.active ? (
        <>
          <div className="rec-live">
            <span className="rec-id" title={rec.run_id ?? ""}>
              <span className="rec-dot" />
              {rec.run_id}
            </span>
            <span className="microlabel">recording</span>
          </div>
          <div className="rec-timer">{new Date(elapsed * 1000).toISOString().slice(11, 19)}</div>
          <div style={{ display: "flex", gap: 6 }}>
            <input
              placeholder="Note / bookmark…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addNote()}
            />
            <button className="btn" disabled={!note.trim()} onClick={addNote}>
              Note
            </button>
          </div>
          {noteMsg && <div className="rec-note-msg">{noteMsg}</div>}
          <button className="btn rec-stop" onClick={doStop} disabled={busy}>
            <span className="sq" />
            Stop recording
          </button>
        </>
      ) : (
        <>
          <input placeholder="Run name" value={name} onChange={(e) => setName(e.target.value)} />
          <input placeholder="Operator" value={operator} onChange={(e) => setOperator(e.target.value)} />
          <input placeholder="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
          <button className="btn rec-start" onClick={doStart} disabled={busy}>
            <span className="rdot" />
            Start recording
          </button>
        </>
      )}
      {err && <div className="err-text">{err}</div>}
    </div>
  );
}
