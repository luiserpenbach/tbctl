-- CRC-16/CCITT-FALSE (spec D8): poly 0x1021, init 0xFFFF, no reflect, xorout 0x0000.
-- Must remain bit-identical to src/tbctl/crc.py; both are checked against
-- tests/golden/crc_vectors.txt.
--
-- Uses only the T7 firmware `bit` library (bit.band/bit.bxor/bit.rshift) and
-- arithmetic that is valid on the T7's Lua as well as desktop Lua 5.3/5.4
-- (see tests/lua/bit_compat.lua for the desktop shim). No string ops, and the
-- 256-entry table is built once at load time -- the per-byte update is
-- allocation-free, so it is safe to call from the LOAD state of the
-- sequencer (never from the 1 ms RUNNING loop).

local CRC_TABLE = {}
for b = 0, 255 do
  local c = b * 256
  for _ = 1, 8 do
    if bit.band(c, 0x8000) ~= 0 then
      c = bit.band(bit.bxor(c * 2, 0x1021), 0xFFFF)
    else
      c = bit.band(c * 2, 0xFFFF)
    end
  end
  CRC_TABLE[b] = c
end

-- bytes: 1-based array of byte values (0..255); n: explicit length
-- (no '#' so the same code runs on T7 firmware Lua); init: optional running
-- CRC for incremental use (default 0xFFFF).
local function crc16(bytes, n, init)
  local crc = init or 0xFFFF
  for i = 1, n do
    local idx = bit.band(bit.bxor(bit.rshift(crc, 8), bytes[i]), 0xFF)
    crc = bit.band(bit.bxor(crc * 256, CRC_TABLE[idx]), 0xFFFF)
  end
  return crc
end

return { crc16 = crc16 }
