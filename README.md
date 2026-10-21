# tbctl — Hopper test control & mission control

DAQ + valve control + deterministic sequencing for the Hopper programme:
the H2 lander and the Aldenhoven test bench (cold flows, igniter tests,
engine hot fires), with a Sift-style web UI. Recorded runs export straight
into [hopper-test-lab](https://github.com/luiserpenbach/hopper-test-lab)
for analysis and reporting (`docs/hopper-test-lab.md`).

> **Before the next hardware test, work through [`TODO.md`](TODO.md).** The
> configs in `configs/` are generic examples, not the Hopper plumbing.

**Architecture in one paragraph:** A LabJack T7 in the test cabinet drives
~8 on/off valves and igniters (DIO → driver board) and reads ~20 mixed
sensors. A Raspberry Pi 5 (or any Linux server) on the bench LAN runs a
single Python process (`tbctl-node`)
that streams data from the T7, records runs to HDF5, and serves a FastAPI
REST + WebSocket API. Time-critical valve sequences do **not** run in
Python: they are compiled from YAML into a binary step table, uploaded to a
frozen generic Lua interpreter running *on* the T7 (1 ms tick), verified by
CRC read-back, then armed and fired. The operator's laptop is a pure
browser client (React + uPlot). Safety layers: Lua heartbeat abort (host
loss), T7 hardware watchdog (Lua death), hardwired E-stop (outside
software). Valve and igniter states are streamed with the sensors and
recorded as 0/1 channels.

Decisions D1–D12 of the original project spec are frozen.

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
configs/                 # channels.yaml, sequences/, dashboards/ (EXAMPLES — see TODO.md)
tests/                   # pytest + golden CRC vectors + Lua harness
```

## Run the full stack locally (simulator)

```bash
uv run tbctl-node --sim                      # node + API on :8000
cd frontend && npm install && npm run dev    # dashboard on :5173 (proxies to :8000)
```

Or build the frontend once (`npm run build`) and the node serves it itself
at `http://localhost:8000/`.

## Analysis in hopper-test-lab

Each closed run has two extra downloads in the sidebar (`htl⬇`, `map⬇`),
also available as `GET /api/runs/{id}/export/hopper.csv` /
`hopper-channel-map.json` and `uv run tbctl export-hopper data/runs/*.h5`:

- a wide CSV hopper-test-lab's **Imports** reads as-is (absolute time,
  test case ID = run ID, valve edges for ignition delay), and
- a hopper channel map (names, units, channel types) to load once per
  channel configuration.

Channel `name`s (P&ID IDs such as `PT-CC-01`), sequence `test_type`s
(`engine` / `igniter` / `cold_flow`) and run outcomes (`pass` / `anomaly` /
`fail` / `void`) use hopper-test-lab's vocabulary. Contract and workflow:
`docs/hopper-test-lab.md`; numpy/pandas access: `docs/analysis.md`.

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
