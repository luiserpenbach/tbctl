-- Executes the REAL assembled lua/sequencer.lua under desktop Lua (sec 10.1),
-- with MB.*/LJ.* shimmed. The script's main loop calls LJ.CheckInterval once
-- per iteration; the shim yields to this driver, which resumes it with a
-- tick count — so tests control virtual time exactly.
--
-- Run from the repo root:  lua5.4 tests/lua/run_sequencer_tests.lua
-- An optional argument names the script to execute (used by the pytest
-- wrapper to run the MINIFIED deploy artifact through the same suite):
--   lua5.4 tests/lua/run_sequencer_tests.lua /path/to/sequencer.min.lua

local SEQ_PATH = (arg and arg[1]) or "lua/sequencer.lua"

dofile("tests/lua/bit_compat.lua")
local crcmod = dofile("lua/crc16.lua")

-- ---------------------------------------------------------------- constants
-- Compact header uses 1-char keys on U.A / U.K; flatten to C.ADDR_* names.
local AKEY = {
  q = "ADDR_CMD",
  a = "ADDR_ARM_CODE",
  h = "ADDR_HEARTBEAT",
  t = "ADDR_HB_TIMEOUT_MS",
  s = "ADDR_STATE",
  n = "ADDR_STEP_COUNT",
  c = "ADDR_CRC16",
  u = "ADDR_CUR_STEP",
  e = "ADDR_ERROR_CODE",
  v = "ADDR_INTERP_VERSION",
  m = "ADDR_ELAPSED_MS",
  f = "ADDR_FIFO_DATA",
  p = "ADDR_FIFO_ALLOCATE",
  b = "ADDR_FIFO_BYTES_IN",
  y = "ADDR_FIFO_EMPTY",
  d = "ADDR_DIO_STATE",
  i = "ADDR_DIO_INHIBIT",
  o = "ADDR_DIO_DIRECTION",
}
local KKEY = {
  v = "INTERP_VERSION",
  m = "ARM_MAGIC",
  h = "DEFAULT_HB_TIMEOUT_MS",
  g = "DEFAULT_SPARK_GUARD_MS",
  k = "TICK_MS",
  s = "MAX_STEPS",
  p = "MIN_STEP_SPACING_MS",
  b = "STEP_BYTES",
  e = "HEADER_BYTES",
  r = "TRAILER_BYTES",
  a = "ALLOCATE_BYTES",
}
local C = {
  CMD_NOP = 0, CMD_LOAD = 1, CMD_ARM = 2, CMD_FIRE = 3, CMD_ABORT = 4, CMD_RESET = 5,
  ST_IDLE = 0, ST_LOADED = 1, ST_ARMED = 2, ST_RUNNING = 3, ST_DONE = 4,
  ST_ABORTING = 5, ST_ABORTED = 6, ST_FAULT = 7,
  ERR_NONE = 0, ERR_HB_TIMEOUT = 1, ERR_HOST_ABORT = 2, ERR_BAD_TABLE = 3,
  ERR_SPARK_GUARD = 4, ERR_OVERRUN = 5,
}
local current = nil
for line in io.lines("lua/gen/registers_gen.lua") do
  local tname = string.match(line, "^U%.([AK]) = {")
  if tname then
    current = tname
  elseif current then
    if string.match(line, "^}") then
      current = nil
    else
      -- hex first: "m=0xA55A" also matches the decimal pattern (as "0")
      local hkey, hval = string.match(line, "^%s*([%w_]+)=0x(%x+)")
      local key, value = string.match(line, "^%s*([%w_]+)=(%-?%d+)")
      if hkey then
        local map = current == "A" and AKEY or KKEY
        C[map[hkey]] = tonumber(hval, 16)
      elseif key then
        local map = current == "A" and AKEY or KKEY
        C[map[key]] = tonumber(value)
      end
    end
  end
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
  dofile(SEQ_PATH)
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

local function setreg(addr, val) regs[addr] = val end
local function getreg(addr) return regs[addr] or 0 end

local function push_fifo_bytes(bytes)
  -- bytes: array of byte values; pack little-endian u16 words into fifo
  local i = 1
  while i <= #bytes do
    local lo = bytes[i] or 0
    local hi = bytes[i + 1] or 0
    fifo_tail = fifo_tail + 1
    fifo[fifo_tail] = lo + hi * 256
    i = i + 2
  end
end

local function cmd(c)
  setreg(C.ADDR_CMD, c)
  step(1)
end

local checks = 0
local function check(cond, msg)
  checks = checks + 1
  if not cond then
    io.stderr:write("FAIL: " .. msg .. "\n")
    os.exit(1)
  end
end

local function spark_bit(v) -- bit 6 (0x0040) of a DIO word
  return math.floor(v / 64) % 2 == 1
end

-- --------------------------------------------------------------- scenarios
check(getreg(C.ADDR_INTERP_VERSION) == C.INTERP_VERSION, "interp_version published")
check(getreg(C.ADDR_STATE) == C.ST_IDLE, "starts IDLE")

-- Build a minimal valid table: 2 steps, 0 abort, spark_guard=0
local function u16le(n)
  return { n % 256, math.floor(n / 256) % 256 }
end
local function u32le(n)
  return {
    n % 256,
    math.floor(n / 256) % 256,
    math.floor(n / 65536) % 256,
    math.floor(n / 16777216) % 256,
  }
end
local function concat(...)
  local out = {}
  for i = 1, select("#", ...) do
    local t = select(i, ...)
    for j = 1, #t do out[#out + 1] = t[j] end
  end
  return out
end

local inhibit, safe = 0x0000, 0x0000
local header = concat(
  u16le(2), u16le(0), u16le(inhibit), u16le(safe), u16le(0), u16le(0)
)
local steps = concat(
  concat(u32le(10), u16le(0x0001)),
  concat(u32le(20), u16le(0x0003))
)
local body = concat(header, steps)
local crc = crcmod.crc16(body, #body)
local table_bytes = concat(body, u16le(crc))

-- ---------------------------------------------------------------------------
-- C3: FIRST-BOOT FAULT. A rejected first LOAD must leave DIO completely
-- untouched: DIO_INHIBIT/DIO_DIRECTION were never programmed, so the T7
-- power-on default (all lines inputs, inhibit 0) is the only safe state and
-- writing DIO_STATE would drive every line as an output.
-- ---------------------------------------------------------------------------
push_fifo_bytes(concat(body, u16le(crc ~ 1)))
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "first-boot bad table -> FAULT")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "first-boot ERR_BAD_TABLE")
check(#dio_writes == 0, "first-boot FAULT writes no DIO_STATE (C3)")
check(regs[C.ADDR_DIO_INHIBIT] == nil and regs[C.ADDR_DIO_DIRECTION] == nil,
  "first-boot FAULT leaves DIO_INHIBIT/DIO_DIRECTION unprogrammed")
check(getreg(C.ADDR_CRC16) == 0 and getreg(C.ADDR_STEP_COUNT) == 0,
  "FAULT publishes zeroed table identity (no stale CRC/step count)")
cmd(C.CMD_ABORT) -- M11: ABORT in an unconfigured FAULT has nothing safe to write
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "ABORT in first-boot FAULT stays FAULT")
check(#dio_writes == 0, "ABORT in first-boot FAULT writes no DIO_STATE")
cmd(C.CMD_RESET)
check(getreg(C.ADDR_STATE) == C.ST_IDLE, "RESET from first-boot FAULT -> IDLE")
check(#dio_writes == 0, "RESET from first-boot FAULT writes no DIO_STATE")

-- LOAD
push_fifo_bytes(table_bytes)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "LOAD -> LOADED")
check(getreg(C.ADDR_CRC16) == crc, "CRC echoed")
check(regs[C.ADDR_DIO_INHIBIT] == inhibit, "LOAD programs DIO_INHIBIT")
check(regs[C.ADDR_DIO_DIRECTION] == 65535 - inhibit,
  "LOAD programs DIO_DIRECTION as exact 16-bit complement (no bnot)")

-- ARM
setreg(C.ADDR_ARM_CODE, C.ARM_MAGIC)
setreg(C.ADDR_HB_TIMEOUT_MS, 1000)
setreg(C.ADDR_HEARTBEAT, 1)
cmd(C.CMD_ARM)
check(getreg(C.ADDR_STATE) == C.ST_ARMED, "ARM -> ARMED")

-- FIRE and advance through steps
cmd(C.CMD_FIRE)
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "FIRE -> RUNNING")
step(10)
check(#dio_writes >= 1 and dio_writes[#dio_writes].value == 0x0001, "step1 applied")
step(10)
check(dio_writes[#dio_writes].value == 0x0003, "step2 applied")
step(1)
check(getreg(C.ADDR_STATE) == C.ST_DONE, "DONE after last step")

-- RESET
cmd(C.CMD_RESET)
check(getreg(C.ADDR_STATE) == C.ST_IDLE, "RESET -> IDLE")

-- Bad CRC after a previous successful LOAD -> FAULT, safe word driven
fifo_head, fifo_tail = 1, 0
local bad = concat(body, u16le(crc ~ 1))
push_fifo_bytes(bad)
local pre_fault_writes = #dio_writes
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "bad CRC -> FAULT")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "ERR_BAD_TABLE")
check(#dio_writes == pre_fault_writes + 1 and dio_writes[#dio_writes].value == safe,
  "post-config FAULT drives the previous table's safe word")
check(getreg(C.ADDR_CRC16) == 0 and getreg(C.ADDR_STEP_COUNT) == 0,
  "post-config FAULT zeroes published table identity")
-- M11: ABORT while in a configured FAULT re-asserts the safe word, stays FAULT
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "ABORT in FAULT stays FAULT")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "ABORT in FAULT keeps the fault code")
check(#dio_writes == pre_fault_writes + 2 and dio_writes[#dio_writes].value == safe,
  "ABORT in configured FAULT re-asserts the safe word")
cmd(C.CMD_RESET)
check(getreg(C.ADDR_STATE) == C.ST_IDLE, "RESET from FAULT")

-- ---------------------------------------------------------------------------
-- Full safety matrix (restored §10.2 coverage against the dense sequencer)
-- ---------------------------------------------------------------------------
local SAFE = 0x0000

local function build_table(t)
  -- t = {steps={{t,w},...}, abort={{t,w},...}, inhibit, safe, guard, mask}
  local hdr = concat(
    u16le(#t.steps), u16le(#t.abort),
    u16le(t.inhibit or 0xFF80), u16le(t.safe or SAFE),
    u16le(t.guard or 0), u16le(t.mask or 0)
  )
  local rows = {}
  for _, s in ipairs(t.steps) do rows = concat(rows, u32le(s[1]), u16le(s[2])) end
  for _, s in ipairs(t.abort) do rows = concat(rows, u32le(s[1]), u16le(s[2])) end
  local payload = concat(hdr, rows)
  local c = crcmod.crc16(payload, #payload)
  return concat(payload, u16le(c)), c
end

local function load_table(t)
  fifo_head, fifo_tail = 1, 0
  local bytes, c = build_table(t)
  push_fifo_bytes(bytes)
  setreg(C.ADDR_FIFO_ALLOCATE, C.ALLOCATE_BYTES)
  cmd(C.CMD_LOAD)
  return c
end

local function do_arm(hb_timeout)
  setreg(C.ADDR_HB_TIMEOUT_MS, hb_timeout or 500)
  setreg(C.ADDR_HEARTBEAT, 7)
  setreg(C.ADDR_ARM_CODE, C.ARM_MAGIC)
  cmd(C.CMD_ARM)
end

local function beat()
  setreg(C.ADDR_HEARTBEAT, (getreg(C.ADDR_HEARTBEAT) + 1) % 65536)
end

local STD = {
  steps = { { 0, 0x0001 }, { 50, 0x0003 }, { 100, SAFE } },
  abort = { { 0, SAFE }, { 20, SAFE } },
  guard = 2000, mask = 0x0040,
}

-- lazy FIFO: freed after LOAD drains it
load_table(STD)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "reload after fault ok")
check(getreg(C.ADDR_FIFO_ALLOCATE) == 0, "FIFO freed after LOAD (lazy alloc)")

-- wrong ARM_CODE rejected; FIRE without ARM rejected
setreg(C.ADDR_ARM_CODE, 0x1111)
cmd(C.CMD_ARM)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "wrong ARM_CODE rejected")
cmd(C.CMD_FIRE)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "FIRE without ARM rejected")

-- exact edge timing: tick-by-tick, step@50 lands exactly at t+50
do_arm(500)
check(getreg(C.ADDR_STATE) == C.ST_ARMED, "re-armed with magic")
check(dio_writes[#dio_writes].value == SAFE, "ARM applies safe word")
local scan_from = #dio_writes + 1 -- only look at writes from THIS run
cmd(C.CMD_FIRE)
local fire_tick = now_tick - 1 -- elapsed==1 happened on the FIRE tick
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "re-fired")
for i = 1, 110 do
  step(1)
  if i % 16 == 0 then beat() end
end
local edge50
for k = scan_from, #dio_writes do
  local w = dio_writes[k]
  if w.value == 0x0003 and not edge50 then edge50 = w.tick end
end
check(edge50 == fire_tick + 50,
  "step@50 applied exactly at t+50 ticks (got " .. tostring(edge50 and (edge50 - fire_tick)) .. ")")
check(getreg(C.ADDR_STATE) == C.ST_DONE, "timed run completes")
check(dio_writes[#dio_writes].value == SAFE, "ends at safe word")
cmd(C.CMD_RESET)

-- catch-up: one huge tick batch still applies every step in order
load_table(STD)
do_arm(500)
cmd(C.CMD_FIRE)
step(200)
check(getreg(C.ADDR_STATE) == C.ST_DONE, "catch-up batch reaches DONE")
check(dio_writes[#dio_writes].value == SAFE, "catch-up ends safe")
cmd(C.CMD_RESET)

-- host abort mid-run runs the abort table then holds safe word
load_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, SAFE }, { 20, SAFE } },
  guard = 2000, mask = 0x0040,
})
do_arm(500)
cmd(C.CMD_FIRE)
step(20); beat()
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "long run in progress")
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_ABORTING or getreg(C.ADDR_STATE) == C.ST_ABORTED, "host ABORT honored")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "ERR_HOST_ABORT")
step(30)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "abort table done -> ABORTED")
check(dio_writes[#dio_writes].value == SAFE, "held at safe word after abort")
cmd(C.CMD_RESET)

-- heartbeat loss while RUNNING: 1 ms ticks (not a stall batch) -> HB abort
load_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, SAFE }, { 20, SAFE } },
  guard = 2000, mask = 0x0040,
})
do_arm(500)
cmd(C.CMD_FIRE)
for i = 1, 4 do step(100); beat() end
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "alive with heartbeats")
for i = 1, 600 do step(1) end -- silence > 500 ms
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HB_TIMEOUT, "hb loss -> ERR_HB_TIMEOUT")
step(100)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "hb loss ends ABORTED")
cmd(C.CMD_RESET)

-- heartbeat loss while ARMED
load_table(STD)
do_arm(500)
check(getreg(C.ADDR_STATE) == C.ST_ARMED, "armed for hb test")
for i = 1, 700 do step(1) end
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "armed hb loss -> ABORTED")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HB_TIMEOUT, "armed hb loss error code")
cmd(C.CMD_RESET)

-- spark guard while RUNNING: bit 6 held high past guard -> forced low + abort.
-- Scoped to THIS scenario's writes (the old cumulative scan was tautological).
load_table({
  steps = { { 0, 0x0040 }, { 5000, SAFE } },
  abort = { { 0, SAFE } },
  guard = 100, mask = 0x0040,
})
do_arm(500)
local spark_scan = #dio_writes + 1
cmd(C.CMD_FIRE)
local spark_fire = now_tick -- spark word applied on the FIRE tick itself
for i = 1, 50 do step(1) end
beat()
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "spark on, pre-guard still running")
for i = 1, 60 do step(1) end
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_SPARK_GUARD, "guard trip -> ERR_SPARK_GUARD")
local seen_on, forced_at = false, nil
for k = spark_scan, #dio_writes do
  local w = dio_writes[k]
  if spark_bit(w.value) then
    seen_on = true
  elseif seen_on and not forced_at then
    forced_at = w.tick
  end
end
check(seen_on, "spark bit observed high in this scenario")
check(forced_at ~= nil, "spark bit forced low after being high")
check(forced_at == spark_fire + 100,
  "force-low lands when max-on exceeded (got +" .. tostring(forced_at and (forced_at - spark_fire)) .. ")")
step(10)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "spark guard ends ABORTED")
check(not spark_bit(dio_writes[#dio_writes].value), "final word has spark low")
cmd(C.CMD_RESET)

-- abort while ARMED goes straight to safe/ABORTED
load_table(STD)
do_arm(500)
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "abort while armed -> ABORTED")
check(dio_writes[#dio_writes].value == SAFE, "armed abort applies safe word")
cmd(C.CMD_RESET)

-- corrupted trailing byte -> FAULT (odd byte is zero-padded to a full word,
-- so the word count still matches and the CRC check is what catches it)
fifo_head, fifo_tail = 1, 0
local tb = build_table(STD)
table.remove(tb) -- drop final byte
push_fifo_bytes(tb)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "corrupted trailing byte -> FAULT (CRC)")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "trailing-byte error code")
cmd(C.CMD_RESET)

-- REAL truncation: drop one FULL word so the declared step counts no longer
-- match the word count -- must trip the length check, not the CRC
fifo_head, fifo_tail = 1, 0
local tw = build_table(STD)
table.remove(tw)
table.remove(tw) -- two bytes = one u16 word
push_fifo_bytes(tw)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "word-truncated upload -> FAULT (length check)")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "word-truncation error code")
cmd(C.CMD_RESET)

-- header rejection: declared n_steps > MAX_STEPS (with no rows present)
local function load_raw(bytes)
  fifo_head, fifo_tail = 1, 0
  push_fifo_bytes(bytes)
  setreg(C.ADDR_FIFO_ALLOCATE, C.ALLOCATE_BYTES)
  cmd(C.CMD_LOAD)
end
local hdr_ns = concat(u16le(C.MAX_STEPS + 1), u16le(0), u16le(0), u16le(SAFE), u16le(0), u16le(0))
load_raw(concat(hdr_ns, u16le(crcmod.crc16(hdr_ns, #hdr_ns))))
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "header ns > MAX_STEPS rejected")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "ns-overflow error code")
check(getreg(C.ADDR_FIFO_ALLOCATE) == 0, "FIFO freed after ns-overflow reject")
cmd(C.CMD_RESET)

-- header rejection: declared n_abort_steps > MAX_STEPS
local hdr_na = concat(u16le(0), u16le(C.MAX_STEPS + 1), u16le(0), u16le(SAFE), u16le(0), u16le(0))
load_raw(concat(hdr_na, u16le(crcmod.crc16(hdr_na, #hdr_na))))
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "header na > MAX_STEPS rejected")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "na-overflow error code")
cmd(C.CMD_RESET)

-- step time above the 1-hour cap rejected (otherwise-valid table, good CRC)
load_table({ steps = { { 3600001, SAFE } }, abort = {}, guard = 0, mask = 0 })
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "step t > 3600000 ms rejected")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "time-cap error code")
cmd(C.CMD_RESET)

-- L5: LOAD while not IDLE is refused but must drain + free the FIFO
local held_crc = load_table(STD)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "loaded for not-idle LOAD test")
fifo_head, fifo_tail = 1, 0
push_fifo_bytes((build_table(STD)))
setreg(C.ADDR_FIFO_ALLOCATE, C.ALLOCATE_BYTES)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "LOAD outside IDLE refused (state unchanged)")
check(fifo_bytes() == 0, "LOAD outside IDLE drains the FIFO")
check(getreg(C.ADDR_FIFO_ALLOCATE) == 0, "LOAD outside IDLE frees the FIFO allocation")
check(getreg(C.ADDR_CRC16) == held_crc, "refused LOAD leaves loaded table identity intact")
cmd(C.CMD_RESET)

-- M6: RESET from ARMED is a clean stand-down: safe word + IDLE, no abort
local SAFE5 = 0x0005
load_table({
  steps = { { 0, 0x0001 }, { 20, SAFE5 } },
  abort = { { 0, SAFE5 } },
  guard = 2000, mask = 0x0040, safe = SAFE5,
})
do_arm(500)
check(getreg(C.ADDR_STATE) == C.ST_ARMED, "armed for stand-down test")
local pre_reset = #dio_writes
cmd(C.CMD_RESET)
check(getreg(C.ADDR_STATE) == C.ST_IDLE, "RESET from ARMED -> IDLE (clean stand-down)")
check(#dio_writes == pre_reset + 1 and dio_writes[#dio_writes].value == SAFE5,
  "stand-down applies the safe word")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_NONE, "stand-down leaves no error")

-- M11: ABORT in DONE drives the safe word and lands in ABORTED
load_table({
  steps = { { 0, 0x0001 }, { 10, SAFE5 } },
  abort = { { 0, SAFE5 } },
  guard = 2000, mask = 0x0040, safe = SAFE5,
})
do_arm(500)
cmd(C.CMD_FIRE)
step(15)
check(getreg(C.ADDR_STATE) == C.ST_DONE, "ran to DONE for late-abort test")
local pre_late = #dio_writes
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "ABORT in DONE -> ABORTED")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "ABORT in DONE error code")
check(#dio_writes == pre_late + 1 and dio_writes[#dio_writes].value == SAFE5,
  "ABORT in DONE drives the safe word")
cmd(C.CMD_RESET)

-- H6: overrun (stall > 250 ms) while RUNNING starts an OVERRUN abort AND the
-- rest of the catch-up batch is dropped so abort steps pace in real time
load_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, 0x0002 }, { 40, SAFE } },
  guard = 2000, mask = 0x0040,
})
do_arm(500)
cmd(C.CMD_FIRE)
step(5); beat()
check(getreg(C.ADDR_STATE) == C.ST_RUNNING, "running before overrun stall")
local pre_stall = #dio_writes
step(300) -- single stalled batch > 250 ticks
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "overrun -> ABORTING, backlog dropped (not collapsed to ABORTED)")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_OVERRUN, "overrun error code")
check(#dio_writes == pre_stall + 1 and dio_writes[#dio_writes].value == 0x0002,
  "exactly one abort tick ran inside the overrun batch")
for i = 1, 45 do step(1) end -- abort step@40 at real-time pacing
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "overrun abort completes")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_OVERRUN, "overrun code preserved to ABORTED")
check(dio_writes[#dio_writes].value == SAFE, "overrun abort ends safe")
cmd(C.CMD_RESET)

-- H6 regression: a stall while ALREADY ABORTING must not restart the abort
-- table and must not overwrite the original abort reason with OVERRUN
load_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, 0x0002 }, { 500, SAFE } },
  guard = 2000, mask = 0x0040,
})
do_arm(500)
cmd(C.CMD_FIRE)
step(20); beat()
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "staged host abort in progress")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "host abort reason recorded")
check(dio_writes[#dio_writes].value == 0x0002, "abort step 1 applied once")
local pre_abort_stall = #dio_writes
step(300) -- stall DURING the abort: > 250 ticks in one batch
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "stall during ABORTING does not restart or finish the abort")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT,
  "stall during ABORTING preserves the original error code (no OVERRUN overwrite)")
check(#dio_writes == pre_abort_stall, "no abort words replayed during the stall")
check(getreg(C.ADDR_CUR_STEP) == 2, "abort step index not reset by the stall")
step(300) -- reach step@500
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "staged abort completes after the stall")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "host-abort code survives to ABORTED")
check(dio_writes[#dio_writes].value == SAFE, "staged abort ends safe")
cmd(C.CMD_RESET)

-- L6: the x>1000 tick clamp must not hide elapsed time from the ARMED
-- heartbeat age -- a 5 s stall with a 3 s timeout must still abort
load_table(STD)
do_arm(3000)
check(getreg(C.ADDR_STATE) == C.ST_ARMED, "armed for clamp test")
step(5000) -- clamped to 1000 processed ticks; full 5000 ms must count as age
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "clamped stall still detects heartbeat loss (L6)")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HB_TIMEOUT, "clamped-stall error code")
for i = 1, 30 do step(1) end
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "clamped-stall abort completes")
check(dio_writes[#dio_writes].value == SAFE, "clamped-stall abort ends safe")
cmd(C.CMD_RESET)

-- L7: spark guard stays active during ABORTING on the commanded word; a trip
-- forces the bit low but neither restarts the abort nor changes its reason
load_table({
  steps = { { 0, 0x0001 }, { 5000, SAFE } },
  abort = { { 0, 0x0041 }, { 400, SAFE } }, -- abort word energizes the spark bit
  guard = 100, mask = 0x0040,
})
do_arm(500)
cmd(C.CMD_FIRE)
step(10); beat()
cmd(C.CMD_ABORT)
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "aborting with spark-energizing abort word")
check(spark_bit(dio_writes[#dio_writes].value), "abort word set the spark bit")
for i = 1, 99 do step(1) end -- just below the 100 ms max-on
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "pre-trip error code intact")
local pre_trip = #dio_writes
for i = 1, 5 do step(1) end -- crosses the max-on threshold
check(#dio_writes == pre_trip + 1 and dio_writes[#dio_writes].value == 0x0001,
  "spark bit forced low during ABORTING (0x0041 -> 0x0001)")
check(getreg(C.ADDR_STATE) == C.ST_ABORTING, "guard trip does not restart or exit the abort")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_HOST_ABORT, "guard trip during ABORTING preserves the error code")
step(500)
check(getreg(C.ADDR_STATE) == C.ST_ABORTED, "abort completes after spark force-low")
check(dio_writes[#dio_writes].value == SAFE, "spark-abort scenario ends safe")
cmd(C.CMD_RESET)

-- full §5 capacity: 128 main + 128 abort steps must LOAD and run (the old
-- byte-buffer parse OOMed the real T7 at 100 steps; streaming parse must not)
local big_steps, big_abort = {}, {}
for i = 1, 128 do big_steps[i] = { (i - 1) * 2, (i % 2 == 0) and SAFE or 0x0001 } end
for i = 1, 128 do big_abort[i] = { (i - 1) * 2, SAFE } end
big_steps[128] = { 127 * 2, SAFE } -- end safe
load_table({ steps = big_steps, abort = big_abort, guard = 2000, mask = 0x0040 })
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "128+128 step table LOADs (streaming parse)")
check(getreg(C.ADDR_STEP_COUNT) == 128 * 256 + 128, "full-capacity step count")
do_arm(500)
cmd(C.CMD_FIRE)
for i = 1, 30 do step(10); beat() end
check(getreg(C.ADDR_STATE) == C.ST_DONE, "full-capacity table runs to DONE")
check(dio_writes[#dio_writes].value == SAFE, "full-capacity table ends safe")
cmd(C.CMD_RESET)

print(string.format("lua sequencer: %d/%d checks OK (%s executed)", checks, checks, SEQ_PATH))
