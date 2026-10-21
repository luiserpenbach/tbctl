# Sift feature survey → ranked tbctl backlog

Survey of the Sift platform (siftstack.com, docs.siftstack.com, github.com/sift-stack,
August 2026), filtered through what actually helps a single engine testbench with a
small team. Ranking weighs test-day/review value against implementation cost on our
stack (FastAPI + HDF5 + uPlot; no cloud, runs on a bench Pi).

Sift's platform in one line: Assets/Runs/Channels data model → ingestion from many
formats → Explore workspace (panels, overlays, time-aligned logs/video) → automated
review (CEL rules → annotations → reports) → governance (versioning, lineage, RBAC)
→ Sift Agents (AI review).

---

## Tier 1 — high value, clearly worth building

### 1. Run comparison / overlay
**Sift:** overlay hundreds of runs in one view, aligned on any event ("overlay today's
run on last month's to see exactly what changed").
**tbctl:** overlay 2–5 runs on one chart, time-aligned on `seq_fired` (t=0 = FIRE)
instead of wall clock. This is *the* post-test question — "was ignition earlier/harder
than last time?" — and we already store `seq_fired` in every run's event log, so
alignment is trivial. Feasibility: high (frontend-heavy; `/api/runs/{id}/data`
already serves the series).

### 2. Calculated channels (derived signals)
**Sift:** CEL expressions over channels become saved channels ("power = V × I"),
stateless or stateful, reusable in rules and plots.
**tbctl:** `configs/derived.yaml` — e.g. `dp_injector = pressure.ox_manifold -
pressure.chamber`, mass flow from ΔP, thrust coefficient, rolling means. Computed in
the acquisition path so they appear in live charts, the recorder, redlines, and CSV
export like any other channel. Safe expression subset (no CEL dependency: a small
vetted AST evaluator). Feasibility: high; touches acquisition + config only.

### 3. Post-run rules → automated run report
**Sift:** Rules (CEL) evaluated per run generate annotations and roll up into Reports
via report templates — the same detection logic applied to every run of a campaign.
**tbctl:** extend the redline concept into non-aborting *review rules* (per sequence
or global): max chamber pressure, ignition delay < X ms, purge pressure held, sensor
dropout counts. Evaluated automatically at `run stop`, stored in the run's HDF5 +
shown as a pass/fail report card on the run view, exportable. Turns "scroll through
plots after every test" into "read the report, investigate the reds". Feasibility:
medium — rule schema + evaluator over recorded arrays + report UI.

### 4. Annotations with status workflow
**Sift:** annotations mark time points/ranges, carry triage status, can be assigned
and commented on; they're searchable institutional memory.
**tbctl:** upgrade run notes to time-*range* annotations with a label, severity and
open/resolved status, editable from the chart (drag-select → annotate). Rules from #3
create annotations automatically. Feasibility: medium; builds directly on the
existing note/event mirror.

---

## Tier 2 — solid value, do after Tier 1

### 5. Shareable view links (URL state)
**Sift:** shared links retain channels, time range, filters and annotations.
Encode dashboard/run/time-window/selected-channels in the URL hash so a link pasted
in chat reproduces the exact view on another laptop in the control room. Cheap.

### 6. Run tags & searchable run list
**Sift:** rich metadata on runs; instant search across history.
Add free-form tags + fields (test article s/n, campaign, outcome good/anomaly/scrub)
to run metadata, with filter/search in the runs sidebar. Cheap, and the value
compounds as the archive grows.

### 7. Time-aligned event/log lane under charts
**Sift:** unified timeline of signals, logs and events.
Render the run's event log (valve commands, seq steps, notes, aborts) as a dedicated
lane under time-series panels, cursor-synced. We already draw markers; a lane with
labels makes reviews much faster.

### 8. Scatter / XY panel
**Sift:** multiple panel types beyond time series (scatter etc.).
XY plots (thrust vs Pc, ΔP vs flow) for steady-state characterization, from live
buffers or a run window. Moderate effort with uPlot.

### 9. Python analysis helper
**Sift:** client libraries (Python/Rust/Go) + Parquet export for offline/ML analysis.
A tiny `tbctl_py` module (or documented snippet) that loads a run HDF5 into pandas
with channels/metadata resolved — cheap, and makes the archive useful in notebooks.

---

## Tier 3 — interesting, revisit when the need is felt

### 10. Video capture time-aligned with telemetry
**Sift:** video/images ingested and reviewed on the same timeline.
A test-cam (e.g. Pi camera / IP cam) recorded per run and scrubbed next to the
charts would be genuinely valuable for hotfires — but sync, storage and capture
hardware make it a project of its own.

### 11. Streaming stateful analysis (drift/spectral monitors)
**Sift:** windowed statistical operators on live streams (drift, change detection).
Live rolling std/drift monitors could catch slow leaks pre-test. Our redlines +
calculated channels (Tier 1) cover most of this once combined.

### 12. Governance: versioned rules/config lineage, audit trail
**Sift:** version control + lineage on rules/annotations/calculations, RBAC, audit.
Our configs are git-versioned with SHA256s stamped into runs, and `.backups` covers
edits — good enough at this scale. Multi-user auth/spectator mode stays future.

### 13. External rules API / CI hooks
**Sift:** rules evaluated from CI/CD or HIL pipelines via API.
Could run review rules against sim runs in GitHub Actions as regression tests of
sequences. Neat, not urgent.

### 14. Multi-asset / fleet model
Sift's Assets abstraction is for fleets of vehicles/stands. One bench = skip.

### 15. AI agent review ("Sift Agents")
Agentic anomaly investigation over runs. Far out of scope for now; the structured
report (#3) is the prerequisite that would make any future assistant useful.

---

## Suggested build order

| Order | Item | Size |
|---|---|---|
| 1 | Calculated channels (#2) | M |
| 2 | Post-run rules + report (#3) | M/L |
| 3 | Run overlay/comparison (#1) | M |
| 4 | Annotations w/ status (#4) | M |
| 5 | View links (#5) + run tags (#6) | S |
| 6 | Event lane (#7) + XY panel (#8) | S/M |

Rationale: #2 before #3 because review rules want derived signals as inputs; #1 is
independent and can be slotted anywhere for a quick win.
