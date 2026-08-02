import { useEffect, useState } from "react";
import { fetchRunReport, rerunRunReport, type RunReport } from "../api";
import { useStore } from "../store";

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
  const [report, setReport] = useState<RunReport | null>(null);
  const [msg, setMsg] = useState("");
  const runId = viewMode.mode === "run" ? viewMode.run.run_id : null;

  useEffect(() => {
    setReport(null);
    setMsg("");
    if (runId)
      fetchRunReport(runId)
        .then((r) => setReport(r.report))
        .catch((e) => setMsg(String(e)));
  }, [runId]);

  if (!runId) return null;

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
      {msg && <span style={{ fontSize: 10, color: "var(--amber)" }}>{msg}</span>}
    </div>
  );
}
