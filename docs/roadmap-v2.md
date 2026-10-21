# tbctl v2 Roadmap — Operator UX & Configurability

Goal: make tbctl usable for real test campaigns without touching YAML files
or restarting the node. The safety architecture (D1–D12, Lua sequencer,
watchdog chain) is **frozen** — everything below is host/UI-side and
additive.

## Ground rules (apply to every phase)

- **YAML stays the source of truth (D12).** Every UI editor round-trips
  through the API: `GET` returns the parsed model, `POST` validates with
  the existing Pydantic models, writes the YAML file, and hot-applies where
  safe. SHA256-into-run-metadata keeps working unchanged.
- **Config writes are gated**: rejected unless sequencer is `IDLE` and no
  run is recording. Every write emits an event and snapshots the previous
  file to `configs/.backups/<file>.<utc-timestamp>.yaml` (keep last 20).
- **No control over WebSocket** (D6) — all new control/config surfaces are
  REST.
- Each phase is independently shippable and demo-able in `--sim`, in the
  established milestone style (tests + browser verification before merge).

---

## Phase 9 — Dashboard management (S/M)

Today: panels come from `configs/dashboards/*.yaml`; edit mode only
drags/resizes. Missing: add, remove, configure, multiple dashboards.

- **Add panel**: in edit mode, an "add panel" button opens a palette
  (timeseries / value_table / valve_panel / sequence_panel /
  recorder_panel / new types below). Timeseries + value_table get a
  channel-picker (tree with checkboxes, glob input for value_table).
- **Remove panel**: close ✕ on each panel header in edit mode.
- **Configure panel**: gear on panel header → channel list, y-axis
  auto/manual + bounds, title. (Schema already supports all of this.)
- **Multiple dashboards**: tab bar (e.g. `hotfire_main`, `coldflow`,
  `checkout`); create/duplicate/rename/delete → existing
  `GET/POST /api/dashboards` plus a `DELETE`. Last-open dashboard
  remembered in localStorage.
- **Fix/verify drag ergonomics**: drag handle covers the header, resize
  handle visible; panels stack predictably (`react-grid-layout`
  `compactType`). Investigate the reported "moving does not work" — likely
  the drag handle is too small / edit-mode affordance unclear; make edit
  mode visually obvious (dashed panel borders).

Backend: `DELETE /api/dashboards/{name}`; nothing else — the save path
exists. Mostly frontend work.

## Phase 10 — Connection manager: sim/real switch + status (M)

Today: `--sim` is a launch flag; the status bar shows a connected dot only.

- **Connection panel / modal** (status-bar click): mode (SIM / USB /
  Ethernet), device identity (serial, firmware, device temp), LJM library
  presence, interpreter version + `sequencer.lua` running?, watchdog
  armed?, stream rate + backlog, last error.
- **API**: `GET /api/connection` (all of the above);
  `POST /api/connection {mode: "sim"|"usb"|"ethernet", ip?}` — switches at
  runtime by tearing down acquisition, swapping the `DeviceInterface`, and
  restarting the stream **under a guard**: sequencer `IDLE`, not
  recording, control mode off. On real-connect failure: report the error
  and stay on the previous interface (never half-switched).
- **Deploy from UI**: button = `probe` + `deploy-lua` equivalents
  (`POST /api/connection/deploy-lua`), with the LUA_DEBUG tail surfaced on
  failure. Ends the "laptop terminal required" era for bench bring-up.
- Status bar chip shows `SIM` / `USB` / `ETH` explicitly (already shows
  SIM).

Implementation note: `NodeContext.iface` becomes swappable behind a small
`DeviceManager` owning iface + acquisition lifecycle; sequencer/recorder/
manual services already take the iface by reference — they get it via the
manager instead.

## Phase 11 — Channel & calibration editor (M/L)

Today: `configs/channels.yaml` is hand-edited; the answer to "where do I
edit channels?" is "in a text editor", which is wrong for bench work.

- **Channels page** (route `/settings/channels`, not a dashboard panel):
  table of all channels (tag, hw, kind, unit, rate group, cal summary,
  live value). Add / edit / delete rows with kind-specific forms
  (valve: fail_safe + polarity; analog: range + cal; TC: unit).
- **Calibration editor**: per-channel cal form for linear / polynomial /
  table with a live preview plot (volts → EU curve) and a "current reading
  through this cal" readout so a known pressure can be sanity-checked
  before saving.
- **Tare/zero button**: one click captures the current raw mean (~2 s) and
  adjusts the cal offset (linear) / c0 (polynomial) so the reading becomes
  zero (or a typed reference value). Logged as an event with old→new cal.
- **API**: `GET /api/config/channels` (model + raw),
  `POST /api/config/channels` (validate → backup → write → hot-apply).
  Hot-apply = rebuild acquisition (stream restart, ring buffers reset) —
  acceptable when idle; the UI warns that live history clears.
- Guard rails: hw-pin conflicts and >16 outputs already rejected by the
  model; editor surfaces validation errors inline per field.

## Phase 12 — Sequence editor (M/L)

Today: sequences are YAML; the panel only selects/loads.

- **Sequence editor page**: list of sequences → editor with
  - metadata (name, description, max_duration, allow_unsafe_end),
  - **steps table**: rows of `t` + per-tag state cells (dropdowns limited
    to output channels), add/insert/delete/reorder rows,
  - preconditions and redlines tables,
  - **timeline preview**: per-valve lanes rendered from the *compiled*
    table (carry-forward words), so what you see is literally what fires —
    reuses the compiler via a new `POST /api/sequences/validate` (body =
    sequence JSON → compile report or per-field errors, nothing written).
  - abort-table editor with the same UX.
- **Save**: `POST /api/sequences/{name}` → validate + compile-check →
  backup → write YAML. Save-as for cloning a sequence.
- Editing is blocked (read-only view) while that sequence is loaded/armed.
- This also delivers the spec's never-built `tbctl simulate` in spirit:
  the timeline preview *is* the dry run.

## Phase 13 — P&ID panel + editor (L; the headline feature)

Per your design: background SVG, components placed on top, linked to
channels; the P&ID becomes a live mimic + control surface.

- **Schema** `configs/pid/*.yaml`:
  ```yaml
  name: feed_system
  background: assets/feed_system.svg     # uploaded, stored under configs/pid/assets/
  elements:
    - {id: v1, type: valve,  channel: valve.ox_main,     x: 0.42, y: 0.31, size: 0.04, rotation: 90}
    - {id: s1, type: sensor, channel: pressure.ox_tank,  x: 0.18, y: 0.12, decimals: 1}
    - {id: t1, type: label,  text: "OX FEED",            x: 0.10, y: 0.05}
  ```
  Positions normalized 0–1 so the panel scales with panel size. Channel
  refs validated against the channel map on load/save.
- **Viewer panel** (`type: pid` in the dashboard schema — additive):
  background SVG + overlaid elements. Valves draw as bowtie symbols
  colored by live state (10 Hz `dio_state` path, same as valve_panel):
  open = green fill, closed = hollow, igniter = bolt icon. Sensors show
  live value + unit, red when outside an optional warn range. Clicking a
  valve issues the existing `/api/manual/valve` with the same gating
  (control mode + sequencer IDLE) and a small click-to-confirm popover —
  same rules as the valve panel, no new control semantics.
- **Editor** (edit mode of the panel or `/settings/pid`): upload/choose
  background, add component from a small palette, drag to position,
  bind to channel from a dropdown, set size/rotation/decimals; save via
  `GET/POST /api/pid[/{name}]` + `POST /api/pid/assets` (SVG upload,
  sanitized: reject scripts/foreignObject).
- Phasing inside the phase: **13a** schema + API + viewer (P&ID authored
  as YAML by hand still demo-able); **13b** the visual editor.

## Phase 14 — Raw channel viewer + sim controls (S)

- **`raw_table` panel**: every configured input as tag / hw pin / raw
  volts / EU value / rate. Acquisition keeps the raw volts block alongside
  EU in the ring path (one extra array per group, negligible memory) and
  `/api/live` + WS frames grow an optional `raw` request flag. Also a
  register peek row (`AIN0`-style one-shot reads via
  `GET /api/connection/read?name=` — handy for wiring checkout on any
  register, not just streamed ones).
- **`sim_panel`** (only rendered when the node runs `--sim`): sliders /
  numeric inputs per input channel driving `/api/sim/set`, plus canned
  scenarios ("nominal pre-fire", "chamber overpressure ramp") for operator
  training and redline demos.

## Phase 15 — Small high-leverage extras (S each, bundle as one phase)

- **Run notes / bookmarks**: a text box (recorder panel) that appends a
  timestamped `note` event during a run (→ JSONL + HDF5); chart overlay
  markers for `note`, `seq_fired`, `seq_abort*`, `redline_tripped` events
  when viewing a run — post-test review becomes self-documenting.
- **CSV export**: `GET /api/runs/{id}/export.csv?tags=&t0=&t1=` for quick
  sharing/spreadsheets (existing HDF5 read path + csv writer).
- **Checklist panel**: `configs/checklists/*.yaml` (list of items);
  checking items logs events; "all checked" state is visible next to ARM —
  procedural discipline without blocking anything.
- **Abort sound**: short audible alert on ABORTING/FAULT (operators look
  away from screens).
- **Stale-data guard**: value_table/PID sensors grey out when their tag
  hasn't updated for >1 s (dead sensor ≠ frozen last value — safety UX).

---

## Suggested order & rationale

| Phase | Size | Why this order |
|---|---|---|
| 9 dashboards | S/M | Everything else lands as panels; daily-use friction |
| 10 connection | M | Needed constantly at the bench; unblocks UI-driven bring-up |
| 14 raw viewer + sim | S | Trivial after 9/10; huge help for wiring checkout |
| 11 channels/cal | M/L | Required before real sensor calibration campaigns |
| 12 sequence editor | M/L | Test-day iteration speed |
| 13 P&ID | L | Depends on nothing new, but biggest; viewer (13a) before editor (13b) |
| 15 extras | S | Sprinkle between phases as breathers |

Explicitly **not** planned (unchanged from v1 scope): auth/multi-user
(spectator mode still future), proportional valves, timeseries DB,
closed-loop control, editing anything while a sequence is armed/running.
