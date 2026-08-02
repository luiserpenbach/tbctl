// Shareable view links (tier 2): the URL hash mirrors the current view —
// dashboard, editor page, selected run and overlays — so a pasted link
// reproduces the same view on another machine in the control room.
//
//   #v=dashboard&d=hotfire_main&r=<run_id>&o=<id1>,<id2>

import { fetchDashboard, fetchRuns } from "./api";
import { useStore } from "./store";

let applying = false;
let applied = false; // idempotent under StrictMode double-invoke / init retry

// captured at module load — the sync subscription rewrites location.hash as
// soon as the store changes, so the arrival hash must be snapshotted first
const initialHash = window.location.hash;

export async function applyHashOnce(): Promise<void> {
  if (applied) return;
  applied = true;
  const h = new URLSearchParams(initialHash.replace(/^#/, ""));
  if ([...h.keys()].length === 0) return;
  applying = true;
  try {
    const s = useStore.getState();
    const view = h.get("v");
    if (view === "dashboard" || view === "channels" || view === "sequences" || view === "pid")
      s.setView(view);
    const dash = h.get("d");
    if (dash) {
      try {
        s.setDashboard(await fetchDashboard(dash));
        localStorage.setItem("tbctl.dashboard", dash);
      } catch {
        /* dashboard gone — ignore */
      }
    }
    try {
      const runId = h.get("r");
      if (runId) {
        const runs = (await fetchRuns()).runs;
        const run = runs.find((x) => x.run_id === runId);
        if (run) {
          s.setViewMode({ mode: "run", run });
          for (const oid of (h.get("o") ?? "").split(",").filter(Boolean)) {
            const o = runs.find((x) => x.run_id === oid);
            // apply, don't toggle: a re-entrant call must not un-apply an
            // overlay that is already on
            if (o && !useStore.getState().overlayRuns.some((x) => x.run_id === o.run_id))
              s.toggleOverlayRun(o);
          }
        }
      }
    } catch {
      /* runs unavailable — keep whatever applied so far */
    }
  } finally {
    applying = false;
  }
}

export function startHashSync(): () => void {
  return useStore.subscribe((s) => {
    if (applying) return;
    const h = new URLSearchParams();
    h.set("v", s.view);
    if (s.dashboard) h.set("d", s.dashboard.name);
    if (s.viewMode.mode === "run") {
      h.set("r", s.viewMode.run.run_id);
      if (s.overlayRuns.length) h.set("o", s.overlayRuns.map((o) => o.run_id).join(","));
    }
    const next = "#" + h.toString();
    if (window.location.hash !== next) history.replaceState(null, "", next);
  });
}
