# AGENTS.md

## Cursor Cloud specific instructions

`tbctl` is a single-product repo for rocket/thruster test-bench control (DAQ +
valve control + deterministic firing sequences). It runs fully **hardware-free**
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
