-- Executes the REAL assembled lua/sequencer.lua under desktop Lua (§10.1),
-- with MB.*/LJ.* shimmed. The script's main loop calls LJ.CheckInterval once
-- per iteration; the shim yields to this driver, which resumes it with a
-- tick count — so tests control virtual time exactly.
--
-- Run from the repo root:  lua5.4 tests/lua/run_sequencer_tests.lua

dofile("tests/lua/bit_compat.lua")
local crcmod = dofile("lua/crc16.lua")

-- ---------------------------------------------------------------- constants
-- Parse addresses/enums/constants out of the generated header so the harness
-- can never drift from the deployed register map.
local C = {}
for line in io.lines("lua/gen/registers_gen.lua") do
  local name, value = string.match(line, "^local ([%w_]+) = (%-?%d+)")
  if name then C[name] = tonumber(value) end
  local hname, hval = string.match(line, "^local ([%w_]+) = 0x(%x+)")
  if hname then C[hname] = tonumber(hval, 16) end
end
assert(C.ADDR_CMD and C.ADDR_STATE and C.ARM_MAGIC, "failed to parse generated header")

-- ------------------------------------------------------------------- shims
local regs = {}          -- addr -> value
local fifo = {}          -- queue of u16 words
local fifo_head = 1
local fifo_tail = 0
local dio_writes = {}    -- {tick, value} log of DIO_STATE writes
local now_tick = 0

local function fifo_bytes()
  return (fifo_tail - fifo_head + 1) * 2
end

MB = {
  R = function(addr, _typ)
    if addr == C.ADDR_FIFO_DATA then
      if fifo_head > fifo_tail then return 0 end
      local w = fifo[fifo_head]
      fifo_head = fifo_head + 1
      return w
    elseif addr == C.ADDR_FIFO_BYTES_IN then
      return fifo_bytes()
    end
    return regs[addr] or 0
  end,
  W = function(addr, _typ, val)
    regs[addr] = val
    if addr == C.ADDR_DIO_STATE then
      dio_writes[#dio_writes + 1] = { tick = now_tick, value = val }
    end
  end,
}

LJ = {
  IntervalConfig = function() end,
  CheckInterval = function()
    return coroutine.yield()
  end,
}

-- ------------------------------------------------------------------ driver
local co = coroutine.create(function()
  dofile("lua/sequencer.lua")
end)

local function resume(...)
  local ok, err = coroutine.resume(co, ...)
  if not ok then
    io.stderr:write("sequencer crashed: " .. tostring(err) .. "\n")
    os.exit(2)
  end
end

resume() -- run startup; parks at the first CheckInterval

local function step(nticks)
  -- one loop iteration consumes one CheckInterval result of nticks ticks
  now_tick = now_tick + nticks
  resume(nticks)
end

local function wr(addr, v) regs[addr] = v end
local function rd(addr) return regs[addr] or 0 end
local function state() return rd(C.ADDR_STATE) end

local failures, checks = 0, 0
local function expect(cond, msg)
  checks = checks + 1
  if not cond then
    failures = failures + 1
    io.stderr:write("FAIL: " .. msg .. "\n")
  end
end

-- ------------------------------------------------------- table construction
local function push_table(t)
  -- t = {steps={{t,w},...}, abort={{t,w},...}, inhibit, safe, guard, mask, corrupt}
  local bytes = {}
  local function b16(v)
    bytes[#bytes + 1] = v % 256
    bytes[#bytes + 1] = math.floor(v / 256) % 256
  end
  local function b32(v)
    bytes[#bytes + 1] = v % 256
    bytes[#bytes + 1] = math.floor(v / 256) % 256
    bytes[#bytes + 1] = math.floor(v / 65536) % 256
    bytes[#bytes + 1] = math.floor(v / 16777216) % 256
  end
  b16(#t.steps); b16(#t.abort)
  b16(t.inhibit or 0xFF80); b16(t.safe or 0)
  b16(t.guard or 0); b16(t.mask or 0)
  for _, s in ipairs(t.steps) do b32(s[1]); b16(s[2]) end
  for _, s in ipairs(t.abort) do b32(s[1]); b16(s[2]) end
  local crc = crcmod.crc16(bytes, #bytes)
  if t.corrupt then crc = bit.bxor(crc, 1) end
  b16(crc)
  -- push as u16 words, little-endian pairs
  for i = 1, #bytes, 2 do
    fifo_tail = fifo_tail + 1
    fifo[fifo_tail] = bytes[i] + (bytes[i + 1] or 0) * 256
  end
  return crc
end

local function reset_to_idle()
  wr(C.ADDR_CMD, C.CMD_RESET)
  step(1)
  expect(state() == C.ST_IDLE, "RESET -> IDLE")
end

local function beat()
  wr(C.ADDR_HEARTBEAT, (rd(C.ADDR_HEARTBEAT) + 1) % 65536)
end

-- ============================================================== test 1: boot
expect(rd(C.ADDR_INTERP_VERSION) == C.INTERP_VERSION, "publishes INTERP_VERSION at boot")
expect(rd(C.ADDR_FIFO_ALLOCATE) == C.ALLOCATE_BYTES, "allocates FIFO at boot")
expect(state() == C.ST_IDLE, "boots IDLE")

-- ==================================================== test 2: happy path
local SAFE = 0x0000
local tbl = {
  steps = { { 0, 0x0001 }, { 50, 0x0003 }, { 100, SAFE } },
  abort = { { 0, SAFE }, { 20, SAFE } },
  safe = SAFE, guard = 2000, mask = 0x0040,
}
local crc = push_table(tbl)
wr(C.ADDR_CMD, C.CMD_LOAD)
step(1)
expect(state() == C.ST_LOADED, "LOAD -> LOADED")
expect(rd(C.ADDR_CRC16) == crc, "device CRC matches host CRC")
expect(rd(C.ADDR_STEP_COUNT) == 3 * 256 + 2, "STEP_COUNT packs n_steps/n_abort")
expect(rd(C.ADDR_DIO_INHIBIT) == 0xFFFF0000 + 0xFF80, "DIO_INHIBIT written at LOAD")

wr(C.ADDR_HB_TIMEOUT_MS, 500)
wr(C.ADDR_HEARTBEAT, 7)
wr(C.ADDR_ARM_CODE, C.ARM_MAGIC)
wr(C.ADDR_CMD, C.CMD_ARM)
step(1)
expect(state() == C.ST_ARMED, "ARM with magic -> ARMED")
expect(dio_writes[#dio_writes].value == SAFE, "ARM applies safe word")

wr(C.ADDR_CMD, C.CMD_FIRE)
step(1) -- consumes FIRE, first RUNNING tick (elapsed=1)
expect(state() == C.ST_RUNNING, "FIRE -> RUNNING")
local fire_tick = now_tick - 1  -- tick at which elapsed was 0
expect(dio_writes[#dio_writes].value == 0x0001, "step@0 applied")
-- drive tick-by-tick so step edges can be timed exactly
for i = 1, 110 do
  step(1)
  if bit.band(i, 15) == 0 then beat() end
end
expect(dio_writes[#dio_writes] ~= nil, "writes recorded")
-- edge timing: the 50 ms step must apply exactly when elapsed reaches 50
local edge50
for _, wrt in ipairs(dio_writes) do
  if wrt.value == 0x0003 and edge50 == nil then edge50 = wrt.tick end
end
expect(edge50 == fire_tick + 50,
  "step@50 applied exactly at t+50 ticks, got offset " .. tostring(edge50 and (edge50 - fire_tick)))
expect(state() == C.ST_DONE, "sequence completes -> DONE")
expect(dio_writes[#dio_writes].value == SAFE, "final word is safe")
reset_to_idle()

-- ==================================================== test 3: corrupt CRC
push_table({ steps = { { 0, 1 } }, abort = { { 0, 0 } }, corrupt = true })
wr(C.ADDR_CMD, C.CMD_LOAD)
step(1)
expect(state() == C.ST_FAULT, "corrupt CRC -> FAULT")
expect(rd(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "error code bad_table")
reset_to_idle()

-- ================================================= test 4: truncated upload
push_table({ steps = { { 0, 1 }, { 10, 0 } }, abort = { { 0, 0 } } })
-- drop the last word to truncate
fifo_tail = fifo_tail - 1
wr(C.ADDR_CMD, C.CMD_LOAD)
step(1)
expect(state() == C.ST_FAULT, "truncated upload -> FAULT")
reset_to_idle()

-- ============================================== test 5: wrong ARM_CODE
push_table(tbl)
wr(C.ADDR_CMD, C.CMD_LOAD); step(1)
expect(state() == C.ST_LOADED, "reload ok")
wr(C.ADDR_ARM_CODE, 0x1111)
wr(C.ADDR_CMD, C.CMD_ARM); step(1)
expect(state() == C.ST_LOADED, "wrong ARM_CODE rejected")
wr(C.ADDR_CMD, C.CMD_FIRE); step(1)
expect(state() == C.ST_LOADED, "FIRE without ARM rejected")

-- ============================================== test 6: heartbeat timeout
-- (fresh long-running table so it is still RUNNING when the host goes quiet)
wr(C.ADDR_CMD, C.CMD_RESET); step(1)
push_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, SAFE }, { 20, SAFE } },
  safe = SAFE, guard = 2000, mask = 0x0040,
})
wr(C.ADDR_CMD, C.CMD_LOAD); step(1)
wr(C.ADDR_HB_TIMEOUT_MS, 500)
wr(C.ADDR_ARM_CODE, C.ARM_MAGIC)
wr(C.ADDR_CMD, C.CMD_ARM); step(1)
expect(state() == C.ST_ARMED, "re-armed")
wr(C.ADDR_CMD, C.CMD_FIRE); step(1)
for i = 1, 4 do step(100); beat() end
expect(state() == C.ST_RUNNING, "alive with heartbeats")
-- silence: 600 x 1 ms ticks with no heartbeat (Lua still ticking normally,
-- so this exercises HB_TIMEOUT, not the batch-stall OVERRUN guard)
for i = 1, 600 do step(1) end
expect(rd(C.ADDR_ERROR_CODE) == C.ERR_HB_TIMEOUT, "hb loss -> ERR_HB_TIMEOUT")
step(100)
expect(state() == C.ST_ABORTED, "hb loss ends ABORTED")
expect(dio_writes[#dio_writes].value == SAFE, "aborted to safe word")
reset_to_idle()

-- ================================================== test 7: spark guard
push_table({
  steps = { { 0, 0x0040 }, { 5000, SAFE } },   -- spark bit 6 on, never off
  abort = { { 0, SAFE } },
  safe = SAFE, guard = 100, mask = 0x0040,
})
wr(C.ADDR_CMD, C.CMD_LOAD); step(1)
wr(C.ADDR_ARM_CODE, C.ARM_MAGIC); wr(C.ADDR_CMD, C.CMD_ARM); step(1)
wr(C.ADDR_CMD, C.CMD_FIRE); step(1)
step(50); beat()
expect(state() == C.ST_RUNNING, "spark on, still running before guard")
step(60); beat()
expect(rd(C.ADDR_ERROR_CODE) == C.ERR_SPARK_GUARD, "guard trip -> ERR_SPARK_GUARD")
local forced_low = false
for _, wrt in ipairs(dio_writes) do
  if bit.band(wrt.value, 0x0040) == 0 and wrt.tick >= now_tick - 61 then forced_low = true end
end
expect(forced_low, "spark bit forced low on guard trip")
step(10)
expect(state() == C.ST_ABORTED, "spark guard ends ABORTED")
reset_to_idle()

-- ==================================================== test 8: host abort
push_table(tbl)
wr(C.ADDR_CMD, C.CMD_LOAD); step(1)
wr(C.ADDR_ARM_CODE, C.ARM_MAGIC); wr(C.ADDR_CMD, C.CMD_ARM); step(1)
wr(C.ADDR_CMD, C.CMD_FIRE); step(1)
step(20); beat()
wr(C.ADDR_CMD, C.CMD_ABORT)
step(1)
expect(state() == C.ST_ABORTING or state() == C.ST_ABORTED, "host ABORT honored")
expect(rd(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "error code host_abort")
step(30)
expect(state() == C.ST_ABORTED, "abort table done -> ABORTED")
expect(dio_writes[#dio_writes].value == SAFE, "held at safe word")

-- ---------------------------------------------------------------- summary
if failures > 0 then
  io.stderr:write(string.format("%d/%d checks failed\n", failures, checks))
  os.exit(1)
end
print(string.format("lua sequencer: %d/%d checks OK (real sequencer.lua executed)", checks, checks))
