# AGENTS.md

## On the bench node (real hardware)

These rules apply on any host that runs the real node: the `tbctl-node`
systemd service is installed (`systemctl status tbctl-node`), the `hw`
extra is installed, or a T7 answers on the bench LAN. Valves, the igniter
and pressurised hardware may be connected, and people may be standing next
to them. When in doubt, assume they are.

**First, every time:** read `curl -s localhost:8000/api/status` and note
`sequencer.state` and `recording.active`. While a sequence is `ARMED` or
`RUNNING`, or a recording is on, only read: no builds, test suites or other
heavy commands (they share the CPU with the acquisition and heartbeat loop).

**Free to do (no need to ask):**
- Read logs (`journalctl -u tbctl-node`, `/data/tbctl/logs`), runs and configs.
- Read-only API calls (`GET /api/...`).
- Edit code and config files in the repo, run `ruff`, `pytest`,
  `tbctl validate`, `tbctl codegen --check`, and a simulator node on another
  port (`--sim --port 8100 --data-dir /tmp/...`). Never point a second
  non-sim node at the T7.
- git: commit, branch, push to a feature branch.

**Ask the operator first, every time, saying exactly what it will do:**
- Any state-changing call to the live node: control mode, manual valves,
  sequence load / arm / fire / abort, recording start / stop, config or
  connection changes through the API. Aborting is the operator's call too.
- Anything that talks to the T7 directly: `tbctl deploy-lua` (writes flash,
  arms the hardware watchdog), `tbctl probe`, `tbctl soak`, LJM scripts.
- Restarting, stopping or reconfiguring the service (a restart drops an
  armed sequence through heartbeat loss and ends a recording), reboot,
  shutdown.
- System changes: network (`nmcli`, firewall), `/etc/fstab`, disks
  (`mkfs` erases), packages, `uv sync`.
- Activating changed configs on the running node.

**Never, even if asked indirectly:**
- Weaken a safety path without the operator naming the exact change:
  redlines, preconditions, interlocks, `fail_safe` / `open_state`,
  `watchdog_timeout_s`, `HB_TIMEOUT_MS`, igniter `max_on_ms`, the abort path.
- Edit `lua/sequencer.lua` by hand (it is generated, see
  `docs/deployment.md` troubleshooting).
- Bind the node to `0.0.0.0` or expose port 8000 beyond the bench LAN: the
  API has no authentication.
- Put secrets (Wi-Fi passwords, tokens) into the repo.

## Cursor Cloud specific instructions

`tbctl` is a single-product repo for Hopper test-bench and mission control (H2
lander, Aldenhoven test bench: DAQ + valve control + deterministic firing
sequences). Recorded runs are analysed in hopper-test-lab
(`docs/hopper-test-lab.md`); keep the export contract there in sync. It runs fully **hardware-free**
via a built-in simulator (`--sim`), which is how it is developed and tested in
this environment. No database or other external backing service is required —
runs are written to HDF5 files on local disk.

Standard commands live in `README.md` (dev/simulator) and `docs/deployment.md`
(production Pi). Prefer those; the notes below only cover non-obvious caveats.

### Components & how to run them (dev/simulator)

- Backend node + REST/WebSocket API (`src/tbctl/`): `uv run tbctl-node --sim`
  serves on `:8000`. Use `--data-dir /tmp/tbctl-runs` to keep HDF5 run files out
  of the repo tree.
- Frontend dashboard (`frontend/`, React + Vite): `cd frontend && npm run dev`
  serves `:5173` and proxies `/api` + `/ws` to `:8000`. The dev server therefore
  requires the backend node to be running.
- Alternatively `cd frontend && npm run build` once, and the node serves the
  built SPA itself at `http://localhost:8000/` — no separate frontend process
  needed.

### Lint / test / build

- Lint: `uv run ruff check .` (config in `pyproject.toml`; `registers_gen.py` is
  excluded).
- Python tests: `uv run pytest` (unit + integration; async auto-mode).
- Lua tests: `lua5.4 tests/lua/run_crc_tests.lua` and
  `lua5.4 tests/lua/run_sequencer_tests.lua`. These need the `lua5.4` interpreter
  (installed via apt, not `uv`); they execute the real on-device sequencer logic.
- Frontend build: `cd frontend && npm run build` (`tsc -b && vite build`).
- Config/codegen checks: `uv run tbctl validate --config-dir configs` and
  `uv run tbctl codegen --check` (verifies generated register constants are in
  sync — run after editing `shared/registers.yaml`).

### Non-obvious caveats

- `uv` installs to `~/.local/bin` (added to `PATH` via `~/.bashrc`); `node`/`npm`
  come from nvm, also sourced by `~/.bashrc`. Run commands in a login shell so
  both resolve.
- The `hw` extra (`labjack-ljm`) is **only** for real LabJack T7 hardware and
  needs the host LJM shared library. Do NOT `uv sync --extra hw` in this
  environment — the simulator replaces all hardware.
- Firing flow (core functionality) requires, in order: enable control mode
  (`POST /api/control-mode {"enabled":true}`), load a sequence
  (`POST /api/sequences/{name}/load`), pass preconditions, arm, then fire. The
  `arm`/`fire` bodies need `{"confirm":"<sequence_name>"}` — the confirm string
  must equal the loaded sequence name.
- Preconditions read live sensor values; in `--sim` mode set them first via
  `POST /api/sim/set {"tag":"pressure.ox_tank","value":9.0}` (this endpoint only
  exists when the node is started with `--sim`), otherwise arming fails with
  "preconditions failed".
- `configs/sim.yaml` (optional, `--sim` only) is a first-order chamber model
  in `ChamberSim` (`src/tbctl/hw/ljm_mock.py`). It adds to `sim/set`
  setpoints, so tests that set values by hand are unaffected.
