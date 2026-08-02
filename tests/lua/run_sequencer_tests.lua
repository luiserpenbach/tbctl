-- Executes the REAL assembled lua/sequencer.lua under desktop Lua (sec 10.1),
-- with MB.*/LJ.* shimmed. The script's main loop calls LJ.CheckInterval once
-- per iteration; the shim yields to this driver, which resumes it with a
-- tick count — so tests control virtual time exactly.
--
-- Run from the repo root:  lua5.4 tests/lua/run_sequencer_tests.lua

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
      local key, value = string.match(line, "^%s*([%w_]+)=(%-?%d+)")
      local hkey, hval = string.match(line, "^%s*([%w_]+)=0x(%x+)")
      if key then
        local map = current == "A" and AKEY or KKEY
        C[map[key]] = tonumber(value)
      elseif hkey then
        local map = current == "A" and AKEY or KKEY
        C[map[hkey]] = tonumber(hval, 16)
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

-- LOAD
push_fifo_bytes(table_bytes)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_LOADED, "LOAD -> LOADED")
check(getreg(C.ADDR_CRC16) == crc, "CRC echoed")

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

-- Bad CRC -> FAULT
fifo_head, fifo_tail = 1, 0
local bad = concat(body, u16le(crc ~ 1))
push_fifo_bytes(bad)
cmd(C.CMD_LOAD)
check(getreg(C.ADDR_STATE) == C.ST_FAULT, "bad CRC -> FAULT")
check(getreg(C.ADDR_ERROR_CODE) == C.ERR_BAD_TABLE, "ERR_BAD_TABLE")

print(string.format("lua sequencer: %d/%d checks OK (real sequencer.lua executed)", checks, checks))
