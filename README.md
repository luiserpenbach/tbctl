# tbctl — Testbench Control Software

DAQ + valve control + deterministic sequencing for GH2/GOX thruster testing
(cold flows, igniter tests, hot fires) with a Sift-style web UI.

**Architecture in one paragraph:** A LabJack T7 in the test cabinet drives
~8 on/off valves (DIO → driver board) and reads ~20 mixed sensors. A
Raspberry Pi 5 in the cabinet runs a single Python process (`tbctl-node`)
that streams data from the T7, records runs to HDF5, and serves a FastAPI
REST + WebSocket API. Time-critical valve sequences do **not** run in
Python: they are compiled from YAML into a binary step table, uploaded to a
frozen generic Lua interpreter running *on* the T7 (1 ms tick), verified by
CRC read-back, then armed and fired. The operator's laptop is a pure
browser client (React + uPlot). Safety layers: Lua heartbeat abort (host
loss), T7 hardware watchdog (Lua death), hardwired E-stop (outside
software).

See `HANDOFF.md`-level details in the project spec; decisions D1–D12 are
frozen.

## Quick start (development, no hardware)

```bash
uv venv --python 3.12 && uv sync
uv run tbctl validate --config-dir configs   # validate all YAML configs
uv run tbctl codegen --check                 # generated register constants in sync?
uv run pytest                                # unit + integration tests
lua5.4 tests/lua/run_crc_tests.lua           # Lua-side CRC golden vectors
```

## Repository layout

```
shared/registers.yaml    # single source of truth: USER_RAM slots, FIFO, enums (D10)
shared/codegen.py        # generates src/tbctl/hw/registers_gen.py + lua/gen/registers_gen.lua
shared/vendor/           # pinned ljm_constants.json (addresses resolved at codegen time)
src/tbctl/               # the node: config, hw, services, api, cli
lua/                     # the frozen T7 Lua interpreter + CRC
frontend/                # React + TypeScript + Vite dashboard
configs/                 # channels.yaml, sequences/, dashboards/
tests/                   # pytest + golden CRC vectors + Lua harness
```

## Run the full stack locally (simulator)

```bash
uv run tbctl-node --sim                      # node + API on :8000
cd frontend && npm install && npm run dev    # dashboard on :5173 (proxies to :8000)
```

Or build the frontend once (`npm run build`) and the node serves it itself
at `http://localhost:8000/`.

## Deployment

See `docs/deployment.md` (fresh Pi → running node in <30 min: static IPs,
LJM install, USB-SSD data path, `tbctl deploy-lua`, systemd unit) and
`docs/timing-report.md` (§10.3 hardware acceptance, required before hot
fires and after any `interp_version` bump).

## Invariants

- **Never** hardcode Modbus addresses — everything flows from
  `shared/registers.yaml` + LJM's `ljm_constants.json` via `tbctl codegen`.
- CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) implemented identically in
  `src/tbctl/crc.py` and `lua/crc16.lua`, cross-checked against
  `tests/golden/crc_vectors.txt`.
- Any change to the Lua interpreter logic or the host↔Lua register
  interface bumps `interp_version` in `shared/registers.yaml` and requires
  re-running the §10.3 hardware acceptance measurements.
