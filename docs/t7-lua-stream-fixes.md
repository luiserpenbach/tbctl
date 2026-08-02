# T7 Lua + stream coexistence fixes

Notes from getting `tbctl-node` running against a real LabJack T7 over USB
(serial `470011571`, firmware ≥ 1.0332). The T7 shares one ~64 KB heap between
the Lua VM, USER_RAM FIFOs, and the stream buffer. Most of the work below is
about fitting inside that budget and deploying reliably.

## Lua sequencer

### Size / VM limits

T7 Lua is harsh in practice:

| Limit | Constraint |
|---|---|
| Script size | ~4 KiB practical compile budget |
| Locals | ≤ 50 |
| Upvalues | ≤ 10 |
| Shared heap | ~64 KB with stream + FIFOs |

`lua/sequencer_main.lua` was rewritten as a dense single-table script:

- One upvalue table `U` holds state, helpers, address map (`U.A`), and constants (`U.K`).
- Enums (`CMD` / `STATE` / `ERROR`) are inlined as numeric literals.
- Keys are 1 character (`U.A.q` for CMD, etc.).
- CRC-16 is inlined into the assembled artifact as `function U.crc16(...)`
  (desktop `lua/crc16.lua` remains for host-side tests).

`shared/codegen.py` emits the compact `U.A` / `U.K` header and assembles
`lua/sequencer.lua`. Source must be ASCII — a `§` character previously broke
`source.encode("ascii")` during deploy.

### Lazy FIFO allocation

Allocating the step-table FIFO (2048 bytes) at Lua startup left too little RAM
for a 9-channel stream → `SYSTEM_MEMORY_BEREFT` / USB `LJME_RECONNECT_FAILED`.

New split:

1. **Lua** does **not** allocate FIFO on boot.
2. **Host** (`SequencerService.load`) writes `FIFO_ALLOCATE` immediately before
   uploading `FIFO_DATA`.
3. **Lua LOAD** drains the FIFO, then writes `FIFO_ALLOCATE = 0` so the heap is
   free again for streaming.

Idle state: Lua running, FIFO = 0, stream buffer available.

## Deploy path (`script_loader` / LJM)

### Correct LJM byte APIs

| Register | Wrong | Correct |
|---|---|---|
| `LUA_SOURCE_WRITE` | `eWriteNameArray` | `eWriteNameByteArray` / `write_name_bytes` |
| `LUA_DEBUG_DATA` | array read | `eReadNameByteArray` / `read_name_bytes` |

Array writes treat values as floats and corrupt the Lua source payload.

### Deploy hardening

- Minify before upload (strip `--` comments, collapse whitespace).
- Release shared RAM first (stop Lua, zero FIFOs, clear AIN_EF, stop stream).
- Purge VM with a cooperative idle script (`LJ.CheckInterval`) — a bare
  `while true do end` wedges USB.
- Wait for `INTERP_VERSION` before declaring success.
- Save to flash **after** a successful start; retry on `INTFLASH_UNAVAILABLE`.
- On deploy failure: soft-reboot via `eWriteAddress(61998, UINT32, 0x4C4Axxxx)`
  (`LjmDevice.reboot`) to reclaim a fragmented heap, then retry.
- `deploy-lua`: brief settle after flash save before watchdog `*_DEFAULT`
  writes; watchdog arm failure is non-fatal (node re-arms on startup).

### USB vs Ethernet

Use `tbctl deploy-lua --usb` / `tbctl probe --usb` on a laptop. Default
`configs/channels.yaml` stays `connection: ethernet` for the Pi bench.

## Stream + acquisition

With the dense sequencer resident, stream buffer size matters:

| `STREAM_BUFFER_SIZE_BYTES` | Result (Lua up, 9 ch @ 50 Hz USB) |
|---|---|
| default / 4096+ | `SYSTEM_MEMORY_BEREFT` (often surfaces as USB reconnect) |
| ≤ 2048 | OK |

`LjmDevice.stream_start` now forces **2048**. Do not open a second LJM handle /
stream probe between `deploy-lua` and `tbctl-node` — it steals the USB session
and can kill the interpreter (`INTERP_VERSION` drops to 0).

## Live WebSocket charts

Backend acquisition was fine; charts looked frozen:

1. **float32 timestamps** — `msgpack.packb(..., use_single_float=True)` collapsed
   Unix times near `1.7e9` (float32 ULP ~128 s). Frontend `tk > lastT` dropped
   every sample. Pack as float64.
2. **wall-clock `t_last`** — emit loop advanced the cursor with `time.time()`
   while ring samples use stream time. When stream lagged wall clock, later
   frames were filtered out. Advance `t_last` from the last sample timestamp
   in each frame instead.

## Operator checklist (USB laptop)

```powershell
$env:Path = "C:\Users\erpen\.local\bin;$env:Path"
# optional: temporarily set configs/channels.yaml connection: usb
uv run tbctl codegen
uv run tbctl deploy-lua --usb
# wait a few seconds; do not open another LJM client
uv run tbctl-node --data-dir data/runs --log-dir logs --host 127.0.0.1 --port 8002
```

Open `http://127.0.0.1:8002/`. Prefer a free port if 8000/8001 are taken.

Verify:

- `GET /api/status` → `sequencer.interp_version == 1`, `acquisition.running`
- WebSocket `/ws` delivers `frame` messages with advancing float timestamps
- After `POST /api/sequences/{name}/load`, FIFO is allocated briefly then freed
  (Lua path); stream should keep running

## Files touched (this work)

| Area | Files |
|---|---|
| Lua logic | `lua/sequencer_main.lua`, assembled `lua/sequencer.lua` |
| Codegen | `shared/codegen.py`, `lua/gen/registers_gen.lua`, `src/tbctl/hw/registers_gen.py` |
| Deploy | `src/tbctl/hw/script_loader.py`, `src/tbctl/cli.py`, `src/tbctl/hw/ljm_iface.py`, `ljm_mock.py` |
| Host FIFO | `src/tbctl/services/sequencer.py` |
| Live WS | `src/tbctl/api/ws.py` |
| Tests | `tests/lua/run_sequencer_tests.lua`, `tests/test_lua_sequencer.py` |
