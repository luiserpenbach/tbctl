-- CRC-16/CCITT-FALSE (spec D8): poly 0x1021, init 0xFFFF, no reflect, xorout 0x0000.
-- Must remain bit-identical to src/tbctl/crc.py; both are checked against
-- tests/golden/crc_vectors.txt.
--
-- Table-free on purpose: the T7 Lua heap cannot afford a 256-entry table on
-- top of the sequencer. LOAD is the only caller; bit-by-bit is fine there.
-- Uses only the T7 firmware `bit` library (see tests/lua/bit_compat.lua).

-- bytes: 1-based array of byte values (0..255); n: explicit length
-- (no '#' so the same code runs on T7 firmware Lua); init: optional running
-- CRC for incremental use (default 0xFFFF).
local function crc16(bytes, n, init)
  local crc = init or 0xFFFF
  for i = 1, n do
    crc = bit.bxor(crc, bytes[i] * 256)
    for _ = 1, 8 do
      if bit.band(crc, 0x8000) ~= 0 then
        crc = bit.band(bit.bxor(crc * 2, 0x1021), 0xFFFF)
      else
        crc = bit.band(crc * 2, 0xFFFF)
      end
    end
  end
  return crc
end

return { crc16 = crc16 }
