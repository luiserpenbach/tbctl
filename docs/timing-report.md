# tbctl Hardware Acceptance — Timing Report (§10.3)

> **Status: TEMPLATE — measurements pending bench time with Luis.**
> Fill in every table below on the real T7 + driver board with a logic
> analyzer on the DIO lines. Re-run this entire report whenever
> `interp_version` changes (see header of `lua/sequencer.lua`).

## Test setup

| Item | Value |
|---|---|
| Date | _____ |
| Operator(s) | _____ |
| tbctl git hash | _____ |
| `interp_version` | _____ (from `/api/status`) |
| `channels.yaml` SHA256 | _____ |
| T7 firmware version | _____ |
| LJM version | _____ |
| Logic analyzer | model + sample rate: _____ |
| Node hardware | Pi 5 / other: _____ |

Deploy with `uv run tbctl deploy-lua`, verify `/api/status` reports the
expected `interp_version`, then run each test.

## 1. Step-time accuracy and jitter (100-step pattern)

Method: compile and fire a 100-step pattern (2 ms minimum spacing mix,
e.g. `configs/sequences/timing_test.yaml` with steps at irregular
intervals), capture all edges on the analyzer, compare each edge timestamp
to nominal (t=0 at the first edge).

**Pass: every edge within 3 ms of nominal.**

| Metric | Value |
|---|---|
| Edges captured | _____ / 100 |
| Max absolute error | _____ ms |
| Mean error | _____ ms |
| Std dev (jitter) | _____ ms |
| Result | PASS / FAIL |

## 2. ABORT-to-safe-state latency

Method A (REST path): trigger `POST /api/sequences/abort` with a scope
line toggled by the test script at request time; measure to the first DIO
edge of the abort table.
**Pass: < 10 ms.**

Method B (Lua-internal path): induce a spark-guard trip (short
`max_on_ms`); measure spark-on duration past the guard threshold.
**Pass: < 3 ms.**

| Path | Latency | Pass? |
|---|---|---|
| REST abort → first abort edge | _____ ms | |
| Spark guard trip → spark low | _____ ms | |

## 3. Ethernet-pull-to-abort latency

Method: while RUNNING with `HB_TIMEOUT_MS=500`, pull the Ethernet cable at
a known analyzer timestamp (toggle a spare line by hand switch wired to the
pull fixture, or timestamp by video). Measure to the first abort-table edge.

**Pass: ≤ HB_TIMEOUT_MS + 2 ticks (502 ms).**

| Trial | Latency | Pass? |
|---|---|---|
| 1 | _____ ms | |
| 2 | _____ ms | |
| 3 | _____ ms | |

## 4. Lua-kill → hardware watchdog

Method: while ARMED, stop the script (`LUA_RUN=0` from a second host or
kill via Kipling). The node's heartbeat keeps Modbus traffic alive, so
ALSO disconnect the node. Confirm all controlled DIO fall to the fail-safe
image after `watchdog_timeout_s` (5 s default).

| Check | Result |
|---|---|
| Outputs at safe_word after timeout | YES / NO |
| Timeout observed | _____ s |

## 5. Power-cycle-while-ARMED

Method: ARM a sequence, cut T7 power, restore. Confirm:

| Check | Result |
|---|---|
| Outputs at fail-safe image immediately at boot | YES / NO |
| `sequencer.lua` auto-starts (flash) and reports IDLE | YES / NO |
| `INTERP_VERSION` correct after boot | YES / NO |

## 6. Soak (§10.4)

8 h streaming at 50 Hz × 20 ch, on the Pi, recording to USB SSD.

| Metric | Value |
|---|---|
| Duration | _____ h |
| Node RSS at start / end | _____ / _____ MB |
| Expected samples (rate × duration) | _____ |
| `sample_counts` in run metadata | _____ |
| Dropped frames | _____ |
| Result | PASS / FAIL |

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Propulsion lead | | | |
| Operator | | | |
