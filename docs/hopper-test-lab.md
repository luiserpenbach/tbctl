# Analysing runs in hopper-test-lab

tbctl records and controls; [hopper-test-lab](https://github.com/luiserpenbach/hopper-test-lab)
analyses, reports and tracks campaigns. This page is the contract between
the two: what tbctl exports, and how it lands in hopper-test-lab. Change
both sides together (`src/tbctl/services/hopper.py` here,
`src/lib/ingest.ts` there).

## Workflow

1. Record the run on the node (start recording → fire → stop).
2. In the sidebar, next to the run: **`htl⬇`** downloads the run as
   `<run_id>.hopper.csv`, **`map⬇`** downloads `<run_id>.channel-map.json`.
   Same files from a shell: `uv run tbctl export-hopper /data/tbctl/runs/<run>.h5 --out .`
3. hopper-test-lab → **Imports** → drop the CSV. Time, test date and the test
   case ID (= run ID) are read from the file.
4. Channel map: once per channel configuration, create it in hopper-test-lab
   from the JSON (it is the exact `POST /api/channel-maps` body; while signed
   in, the browser console can do it:
   `fetch("/api/channel-maps",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(<json>)})`).
   Reference it from the test template, and every later import of the same
   configuration gets names, units and types without any clicking.
5. Analyse as usual: phases are detected on the chamber PT (pick it as the
   reference channel), ignition delay is timed from the recorded valve edges.

## The CSV

```
timestamp,testcaseId,pressure.chamber,...,valve.ox_main,...,calc.dp_ox_inj
1792593699585.755,20261021-144139-HF-sim,0.0334,...,0.0,...,-0.0411
```

| Column | Content | Why |
|---|---|---|
| `timestamp` | epoch **milliseconds**, µs resolution | hopper reads values > 1e11 as absolute epoch ms → correct test date, no "relative time" warning |
| `testcaseId` | tbctl run ID on every row | hopper's test case ID (shown as "case …", stored with the test) |
| one per channel | engineering units, header = channel **tag** | the tag is hopper's "DAQ ID": stable across renames, the key of the channel map |

- All recorded channels, full rate, on the union timebase of all rate
  groups. A slower group's cells are blank between its samples; hopper holds
  the last value, which is what it does for its own long-format files.
- Valves and igniters are **0/1** columns (1 = open / on, polarity already
  applied), measured from the streamed `FIO_EIO_STATE` word at the fastest
  group's rate. hopper's ignition delay uses the earliest rising valve edge
  that is still open mid steady state, so propellant valves win over
  igniter/purge edges.
- Calculated (`derived`) channels are exported as measured data.

## The channel map

`{ "name": "tbctl <run_id>", "testType": "any", "channels": { <tag>: ChannelMeta } }`
with, per channel:

| hopper field | from tbctl |
|---|---|
| `daqId` | `tag` |
| `name` | `name` in `channels.yaml` (P&ID ID such as `PT-CC-01`), else the tag |
| `unit` | `unit`; `degC`/`degF` become `°C`/`°F` |
| `class` | valve / igniter → `valve`; thermocouple → `temperature`; otherwise by unit: bar, psi, Pa… → `pressure`; °C, K → `temperature`; g/s, kg/s, l/min… → `flow`; anything else (N, V…) → `unknown` ("Other") |
| `color`, `visible` | hopper's own trace palette, visible |

Calibration and zeroing are already applied on the node, so no
`gain`/`offset` is set. tbctl's calculated channels never get hopper's
`derived` class: hopper recomputes `derived` channels from its own
expressions and leaves them out when it re-reads the raw file.

## Shared vocabulary

| Concept | tbctl | hopper-test-lab |
|---|---|---|
| test type | sequence `test_type`: `engine`, `igniter`, `cold_flow` (stamped on the run when it fires) | `TestType` |
| outcome | run outcome `pass`, `anomaly`, `fail`, `void` (old files: `good` → `pass`, `scrub` → `void`) | `Outcome` (Nominal, Off-nominal, Failed, Invalid) |
| channel ID / name | `tag` / `name` | `daqId` / `name` |
| procedure | sequence name | `procedure` |
| operator, notes | run start form | `operator`, `notes` |

Test type, sequence, operator and notes are in the run file and in
`GET /api/runs/{id}`, but hopper-test-lab does not read them from the CSV
yet; see `TODO.md` ("hopper-test-lab side").
