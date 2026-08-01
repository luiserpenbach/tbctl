-- Desktop-Lua shim for the LabJack T7 firmware `bit` library.
-- The T7 provides `bit` as a global; desktop Lua 5.3/5.4 has native
-- operators instead. Loaded by test harnesses before dofile'ing any
-- production Lua so the same source runs unmodified in both places.

if bit == nil then
  bit = {
    band   = function(a, b) return a & b end,
    bor    = function(a, b) return a | b end,
    bxor   = function(a, b) return a ~ b end,
    bnot   = function(a) return (~a) & 0xFFFFFFFF end,
    lshift = function(a, n) return (a << n) & 0xFFFFFFFF end,
    rshift = function(a, n) return a >> n end,
  }
end
