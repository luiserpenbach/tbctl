import { useEffect, useState } from "react";
import {
  deleteAnnotation,
  fetchAnnotations,
  patchAnnotation,
  type Annotation,
} from "../api";
import { useStore } from "../store";

// Annotation list for the selected run: triage status (open/resolved),
// severity, delete. Ranges are also drawn as shaded bands on the charts;
// create new ones by drag-selecting on a chart.

export function RunAnnotations() {
  const viewMode = useStore((s) => s.viewMode);
  const annVersion = useStore((s) => s.annotationsVersion);
  const bump = useStore((s) => s.bumpAnnotations);
  const [anns, setAnns] = useState<Annotation[]>([]);
  const runId = viewMode.mode === "run" ? viewMode.run.run_id : null;
  const tStart = viewMode.mode === "run" ? viewMode.run.t_start : 0;

  useEffect(() => {
    setAnns([]);
    if (!runId) return;
    let cancelled = false; // stale-guard (M22): late response from a previous run
    fetchAnnotations(runId)
      .then((r) => {
        if (!cancelled) setAnns(r.annotations);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [runId, annVersion]);

  if (!runId || anns.length === 0) return null;

  return (
    <div className="report-bar" style={{ marginTop: -4 }}>
      <span className="microlabel">Annotations</span>
      {anns.map((a) => (
        <span
          key={a.id}
          className={`ann-row sev-${a.severity} ${a.status === "resolved" ? "resolved" : ""}`}
          title={`${a.source === "rule" ? "auto (review rule)" : "manual"} · ${new Date(a.t0 * 1000).toISOString().slice(11, 19)}`}
        >
          <span className="ann-dot" />
          +{Math.max(0, a.t0 - tStart).toFixed(1)}s {a.label}
          <button
            className="panel-btn"
            title={a.status === "open" ? "mark resolved" : "reopen"}
            onClick={() =>
              patchAnnotation(runId, a.id, {
                status: a.status === "open" ? "resolved" : "open",
              })
                .then(bump)
                .catch(() => {})
            }
          >
            {a.status === "open" ? "✓" : "↺"}
          </button>
          <button
            className="panel-btn"
            title="delete annotation"
            onClick={() =>
              deleteAnnotation(runId, a.id)
                .then(bump)
                .catch(() => {})
            }
          >
            ✕
          </button>
        </span>
      ))}
    </div>
  );
}
