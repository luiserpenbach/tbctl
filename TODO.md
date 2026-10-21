# Before the next hardware test (LabJack T7 + Raspberry Pi / Linux server)

What has to happen before tbctl drives real hardware for Hopper: the H2
lander and the Aldenhoven test bench. Everything that can be checked
without hardware is already covered in `--sim`. The items below need
decisions, real configuration or the bench itself. Work top to bottom:
each section assumes the ones above it are done.

## 0. Decisions (blocking)

- [ ] **Lander scope.** tbctl assumes a wired bench LAN. If the host
      heartbeat stops for 500 ms (`HB_TIMEOUT_MS`), the T7 runs the abort
      table, so every valve goes to its fail-safe state and the engine shuts
      down. That is right for a captive or tethered test. It is not a flight
      abort. Decide whether lander tests stay captive/tethered with the T7
      on the ground (umbilical), or whether the T7 flies (then you need an
      onboard host, a telemetry link, and an abort strategy for link loss).
      "Mission control" for free flight is a separate design task.
- [ ] One T7 shared between the Aldenhoven stand and the lander, or one each?
      Either way, keep **one config directory per setup** and pass it with
      `--config-dir`.
- [ ] Node host: Raspberry Pi 5 in the cabinet, or a Linux server on the
      bench LAN (`docs/deployment.md` covers both).
- [ ] Output budget: at most 16 sequencer outputs, all on FIO0-7 / EIO0-7.
      Count the valves and igniters, and decide the igniter type (spark
      exciter, glow plug, torch) and its maximum on-time (`max_on_ms`).
- [ ] Input budget: the T7 has 14 native AINs (more with a Mux80). Decide
      which channels need 1 kHz (chamber PT for combustion stability?). All
      fast channels share one stream rate.

## 1. Replace the example configs

The files in `configs/` are a generic biprop example that keeps the simulator
and tests running. None of them describes Hopper hardware.

- [ ] `channels.yaml` for each site: tags (`quantity.location`), `name` =
      P&ID ID (the channel name in hopper-test-lab; keep it identical to the
      names used in hopper templates and formulas), units, `hw` pins, AIN
      `range`, calibration from the sensor certificates, rate groups.
- [ ] For every valve, set `fail_safe` and `open_state` from the driver-board
      wiring (NO/NC, energise-to-open). Get these wrong and the valve's
      "safe" state is wrong.
- [ ] `device.ip`, `connection`, `watchdog_timeout_s`; keep
      `record_outputs: true` unless §3 shows a problem.
- [ ] Sequences: the real cold-flow, igniter and hot-fire procedures, each
      with `test_type`, preconditions (N2 and tank pressures, H2 detector
      signal if one is wired), redlines, and an abort table reviewed by the
      propulsion lead. Delete `hotfire_v1` / `coldflow_v1` / `igniter_v1` once
      they are replaced; keep `timing_test` for §3.
- [ ] `review.yaml` rules, `checklists/pre_fire.yaml` (Hopper/Aldenhoven test
      procedure, H2 items), `dashboards/`, and a P&ID with the real feed-system
      SVG (`pid/`).
- [ ] **Software gap (fix before using thermocouples or load cells):** the
      T7 is never told the AIN `range` from `channels.yaml` (no `AIN#_RANGE`
      write), and there is no differential setting (`AIN#_NEGATIVE_CH`). So
      every input streams single-ended at ±10 V. Thermocouples (mV signals)
      and bridge load cells need ±0.1 V and differential wiring to have
      usable resolution. Add a `negative_ch` field, write range and
      negative channel before `eStreamStart`, then verify on the T7.
- [ ] `uv run tbctl validate --config-dir <dir>` passes, and every sequence's
      compile report has been reviewed (the sequence editor shows it).
- [ ] Dry-run the real config in the simulator (`tbctl-node --sim
      --config-dir <dir>`): load, arm, fire and abort every sequence, trip
      each redline with `POST /api/sim/set`, and train operators on it.

## 2. Host setup

- [ ] Install per `docs/deployment.md`: static IPs, LJM for the host
      architecture, `uv sync --extra hw`, data on the USB SSD (not the SD
      card), frontend build, systemd unit pointing at the right config dir.
- [ ] Isolated bench LAN only: the node has **no authentication** (D6).
- [ ] Time sync (chrony/NTP) on the node. Run timestamps are host
      wall-clock, and hopper-test-lab uses them as the test date and to line
      runs up with cameras.
- [ ] `uv run tbctl probe` (connectivity, firmware, serial), then
      `uv run tbctl deploy-lua` (flash + watchdog). The node must report
      `interp_version 1` at start.

## 3. Hardware acceptance (§10.3) — the last report says "re-run required"

`docs/timing-report.md` (2026-08-02, previous bench, USB proxy, no logic
analyser) did not sign off. Re-run `tools/run_timing_report.py` on the
Hopper T7, with a logic analyser on the outputs:

- [ ] Test 1, step timing: the full 100-step `timing_test`, every edge within
      3 ms. It **FAILED** last time (Lua out of memory, only 8/100 steps ran).
      The streaming LOAD parse that fixes this has not run on hardware yet,
      so also check that a full 128 + 128 step table loads.
- [ ] Test 2: abort latency, and the **igniter guard** trip (it was not
      measured last time).
- [ ] Test 3: heartbeat-loss abort with a real Ethernet pull (last time it
      was a USB proxy), ≤ 502 ms.
- [ ] Test 4: Lua killed → hardware watchdog puts the outputs at safe_word.
      It **FAILED** last time; the startup-delay fix is unconfirmed.
- [ ] Test 5: power-cycle while ARMED (never run). Outputs must be fail-safe
      at boot, and `sequencer.lua` must autostart to IDLE.
- [ ] §10.4 soak: 8 h with zero sample loss (`tbctl soak`). It never finished.
- [ ] **New since then:** valve/igniter states are now streamed
      (`FIO_EIO_STATE` in the scan list). Check that LJM accepts it in
      stream mode, and that there is no `SYSTEM_MEMORY_BEREFT` with the full
      channel count, Lua and the FIFO loaded together. Check the recorded
      0/1 edges against the logic analyser (expect within one scan period).
      If it fails, set `record_outputs: false` and open an issue. Analysis
      then loses ignition-delay timing.
- [ ] Fill in the sign-off table.

## 4. Sensor and actuator checkout

- [ ] Every input: zero and span against a reference, calibration
      coefficients, sign, plausible noise; thermocouples against a reference
      (cold-junction compensation uses the T7 temperature); Mux80 channels.
- [ ] Every valve, driven from the UI: physical state = UI state = recorded
      0/1 channel. Check the fail-safe state on loss of power and on abort,
      and that the vents fail open.
- [ ] Igniter: it fires, and the guard cuts it at `max_on_ms`.
- [ ] Hardwired E-stop (outside software) cuts the valve power.
- [ ] Redlines and preconditions on real signals (raise a pressure safely, or
      lower the limit for the check).

## 5. First runs

- [ ] Inert cold flow (N2/water) with the full recording → export → analysis
      chain before any propellant.
- [ ] Igniter tests, then hot fires, each with its checklist.

## 6. Data path to hopper-test-lab

- [ ] Export the first inert run (`htl⬇` / `map⬇`, or `tbctl export-hopper`)
      and import it into hopper-test-lab. Create the channel map from the
      JSON, then one test template per test type that references the map.
      Check the detected phases, the ignition delay and the formulas
      (`docs/hopper-test-lab.md`).
- [ ] Decide where run files are archived (the node SSD is not a backup):
      sync `/data/tbctl/runs` to a server or NAS after each test day.

## 7. hopper-test-lab side (not blocking)

- [ ] Prefill test type, operator, notes and procedure from tbctl. tbctl
      already has them in the run file and in `GET /api/runs/{id}`; hopper
      needs a way to read them (a sidecar JSON, or `#` header lines in the CSV).
- [ ] "Send to hopper-test-lab" straight from the node into the shared
      Imports. This needs token auth on hopper's API (today it is the shared
      password cookie) and the Vercel Blob upload flow.
- [ ] A `force` channel class (load cells currently show under "Other").
- [ ] Use tbctl events (FIRE, abort, notes) as phase hints and plot markers.

## Notes

- Old run files (schema 1, before this change) have no valve columns and
  no channel names. They still export, but hopper then times ignition from
  the pre-run end, not from a valve edge.
- The on-device Lua (`lua/`) is byte-identical to the previous version. Its
  comments still say "spark" for error code 4, which is now `IGNITER_GUARD`
  on the host. The code is untouched, so `interp_version` stays 1.
- The GitHub repository is still called `spark-studio`. Rename it if you
  want; nothing in the code depends on the name.
