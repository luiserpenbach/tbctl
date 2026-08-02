# tbctl Hardware Acceptance — Timing Report (§10.3)

> **Status: EXECUTED 2026-08-02 (USB software proxy) — re-run required.**
> Edge timing via USB `DIO_STATE` poll — **not** a logic analyzer. Re-run
> with LA on FIO4/FIO5 for sign-off. Two failures were traced and fixed
> after this run; both need hardware confirmation:
>
> 1. **Test 1 (OOM)**: the byte-buffer LOAD parse OOMed the T7 heap at 100
>    steps. Replaced with a streaming word-parse + incremental CRC (no byte
>    buffer); the desktop harness now loads and runs full 128+128 tables.
> 2. **Test 4 (watchdog)**: the watchdog's 10 s startup grace exceeded the
>    6 s test window, so it never armed before the runner reconnected
>    (which resets the comm timer) — `valve.purge` (bit 4) stayed energized.
>    Startup delay is now 3 s and the runner waits grace + timeout + 2 s.

## Test setup

| Item | Value |
|---|---|
| Date | 2026-08-02 |
| Operator(s) | agent |
| tbctl git hash | `d675c14` |
| `interp_version` | 1 |
| `channels.yaml` SHA256 | `4589cbad905afc3e74bf402ba933d6686972ab722a35f0831c98deda440e9206` |
| T7 firmware version | 1.0332000255584717 (serial 470011571) |
| LJM version | labjack-ljm |
| Logic analyzer | NONE — USB DIO_STATE poll |
| Node hardware | Windows laptop USB |

## 1. Step-time accuracy and jitter (100-step pattern)

**Pass: every edge within 3 ms of nominal.**

| Metric | Value |
|---|---|
| Edges captured | 8 / 8 (spec 100) |
| Max absolute error | 22.877 ms |
| Mean error | 0.174 ms |
| Std dev (jitter) | 8.581 ms |
| Result | **FAIL** |

Only 8/100 steps runnable (Lua OOM). LA required for official PASS.

## 2. ABORT-to-safe-state latency

| Path | Latency | Pass? |
|---|---|---|
| CMD abort → ABORTING/ABORTED | 1.975 ms | True |
| Spark guard trip → spark low (past 5 ms guard) | _____ ms (on=_____) | True |

Spark end state: ABORTED / SPARK_GUARD

## 3. Ethernet-pull-to-abort latency

Method: `HB_TIMEOUT_MS=500`, stop host heartbeat (USB proxy for cable pull).

**Pass: ≤ 502 ms.**

| Trial | Latency | Pass? |
|---|---|---|
| 1 | 385.696 ms | True |
| 2 | 382.014 ms | True |
| 3 | 383.103 ms | True |

Overall: **PASS**

## 4. Lua-kill → hardware watchdog

| Check | Result |
|---|---|
| Outputs at safe_word after timeout | NO |
| Timeout observed | 6.60 s |
| Result | **FAIL** |

## 5. Power-cycle-while-ARMED

| Check | Result |
|---|---|
| Outputs at fail-safe image immediately at boot | NOT EXECUTED |
| `sequencer.lua` auto-starts (flash) and reports IDLE | NOT EXECUTED |
| `INTERP_VERSION` correct after boot | NOT EXECUTED |

Reason: Manual T7 power cut/restore while ARMED required.

## 6. Soak (§10.4)

| Metric | Value |
|---|---|
| Duration | 8 h (in progress) |
| Expected samples | rate × duration (checked at stop) |
| `sample_counts` | pending until soak completes |
| Loss | pending |
| Result | **RUNNING** — run `20260802-160657-soak` via `tbctl soak --url http://127.0.0.1:8002 --hours 8` |

## Sign-off

| Role | Name | Date | Signature |
|---|---|---|---|
| Propulsion lead | | | |
| Operator | | | |
