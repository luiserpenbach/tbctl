-- Golden-vector harness for lua/crc16.lua (spec D8, §10.1).
-- Run from the repo root:  lua5.4 tests/lua/run_crc_tests.lua
-- Exits 0 iff every vector in tests/golden/crc_vectors.txt matches.

dofile("tests/lua/bit_compat.lua")
local crcmod = dofile("lua/crc16.lua")

local function parse_hex_bytes(s)
  local bytes, n = {}, 0
  if s ~= "-" then
    for pair in string.gmatch(s, "%x%x") do
      n = n + 1
      bytes[n] = tonumber(pair, 16)
    end
    assert(n * 2 == #s, "odd-length hex string: " .. s)
  end
  return bytes, n
end

local failures, total = 0, 0
for line in io.lines("tests/golden/crc_vectors.txt") do
  line = string.gsub(line, "#.*$", "")
  local hex, expect = string.match(line, "^%s*(%S+)%s+(%S+)%s*$")
  if hex then
    total = total + 1
    local bytes, n = parse_hex_bytes(hex)
    local got = crcmod.crc16(bytes, n)
    local want = tonumber(expect, 16)
    if got ~= want then
      failures = failures + 1
      io.stderr:write(string.format(
        "FAIL: crc(%s) = 0x%04X, want 0x%04X\n", hex, got, want))
    end
  end
end

if total == 0 then
  io.stderr:write("no vectors parsed -- bad vectors file?\n")
  os.exit(2)
end
if failures > 0 then
  io.stderr:write(string.format("%d/%d vectors failed\n", failures, total))
  os.exit(1)
end
print(string.format("lua crc16: %d/%d golden vectors OK", total, total))
