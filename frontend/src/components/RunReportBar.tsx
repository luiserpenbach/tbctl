import { useEffect, useState } from "react";
import { fetchRunReport, fetchRuns, patchRunMeta, rerunRunReport, type RunReport } from "../api";
import { useStore } from "../store";

const OUTCOMES = ["good", "anomaly", "scrub"] as const;

// Automated post-run review report (Sift-style): pass/fail per rule for the
// selected run, shown while reviewing it. Rules live in configs/review.yaml.

function fmtVal(v: number | null): string {
  if (v === null) return "—";
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
}

function limitText(r: { limit_min: number | null; limit_max: number | null }): string {
  if (r.limit_min !== null && r.limit_max !== null) return `${r.limit_min}…${r.limit_max}`;
  if (r.limit_max !== null) return `≤ ${r.limit_max}`;
  return `≥ ${r.limit_min}`;
}

export function RunReportBar() {
  const viewMode = useStore((s) => s.viewMode);
  const setRuns = useStore((s) => s.setRuns);
  const setViewMode = useStore((s) => s.setViewMode);
  const [report, setReport] = useState<RunReport | null>(null);
  const [msg, setMsg] = useState("");
  const [tagText, setTagText] = useState("");
  const runId = viewMode.mode === "run" ? viewMode.run.run_id : null;
  const run = viewMode.mode === "run" ? viewMode.run : null;

  useEffect(() => {
    setReport(null);
    setMsg("");
    setTagText(run?.tags?.join(", ") ?? "");
    if (runId)
      fetchRunReport(runId)
        .then((r) => setReport(r.report))
        .catch((e) => setMsg(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  if (!runId || !run) return null;

  // patch meta, refresh the runs list, and keep the selected run in sync
  const saveMeta = (body: Parameters<typeof patchRunMeta>[1]) =>
    patchRunMeta(runId, body)
      .then(async () => {
        const runs = (await fetchRuns()).runs;
        setRuns(runs);
        const fresh = runs.find((r) => r.run_id === runId);
        if (fresh) setViewMode({ mode: "run", run: fresh });
      })
      .catch((e) => setMsg(String(e).replace(/^Error: /, "")));

  const rerun = () =>
    rerunRunReport(runId)
      .then((r) => {
        setReport(r.report);
        setMsg("");
      })
      .catch((e) => setMsg(String(e).replace(/^Error: /, "")));

  return (
    <div className="report-bar">
      <span className="microlabel">Run report</span>
      {report === null && <span className="report-none">no report — </span>}
      {report?.summary && (
        <span
          className={`chip ${report.summary.failed > 0 ? "bad" : "ok"}`}
          title={`sequence: ${report.sequence ?? "none"}`}
        >
          <span className="dot" />
          {report.summary.failed > 0
            ? `${report.summary.failed} FAILED`
            : `${report.summary.passed}/${report.summary.total} PASS`}
        </span>
      )}
      {report?.results.map((r) => (
        <span
          key={r.name}
          className={`report-rule ${r.passed === true ? "pass" : r.passed === false ? "fail" : "skip"}`}
          title={
            r.skipped ??
            `${r.metric}(${r.tag}) over ${r.window} window · limit ${limitText(r)}`
          }
        >
          {r.name} {r.skipped ? "SKIP" : `${fmtVal(r.value)} ${r.passed ? "✓" : "✗"}`}
        </span>
      ))}
      <button className="btn" style={{ padding: "0 8px", fontSize: 9.5 }} onClick={rerun}>
        {report ? "Re-evaluate" : "Evaluate"}
      </button>
      <span className="spacer" style={{ flex: 1 }} />
      {OUTCOMES.map((o) => (
        <button
          key={o}
          className={`btn outcome-btn ${o} ${run.outcome === o ? "active" : ""}`}
          style={{ padding: "0 8px", fontSize: 9.5 }}
          title="run outcome (click again to clear)"
          onClick={() =>
            saveMeta(run.outcome === o ? { clear_outcome: true } : { outcome: o })
          }
        >
          {o}
        </button>
      ))}
      <input
        className="modal-input"
        style={{ width: 170, flex: "none" }}
        placeholder="tags, comma separated"
        value={tagText}
        onChange={(e) => setTagText(e.target.value)}
        onBlur={() => saveMeta({ tags: tagText.split(",").map((t) => t.trim()).filter(Boolean) })}
        onKeyDown={(e) =>
          e.key === "Enter" &&
          saveMeta({ tags: tagText.split(",").map((t) => t.trim()).filter(Boolean) })
        }
      />
      <button
        className="btn"
        style={{ padding: "0 8px", fontSize: 9.5 }}
        title="copy a shareable link to this exact view"
        onClick={() =>
          navigator.clipboard
            .writeText(window.location.href)
            .then(() => setMsg("link copied"))
            .catch(() => setMsg(window.location.href))
        }
      >
        Copy link
      </button>
      {msg && <span style={{ fontSize: 10, color: "var(--amber)" }}>{msg}</span>}
    </div>
  );
}
