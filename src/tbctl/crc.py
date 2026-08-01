"""CRC-16/CCITT-FALSE (spec D8): poly 0x1021, init 0xFFFF, no reflection, xorout 0x0000.

Must remain bit-identical to lua/crc16.lua. Both are cross-checked against
the golden vectors in tests/golden/crc_vectors.txt — change nothing here
without updating those vectors and the Lua implementation together.
"""

from __future__ import annotations

_TABLE: list[int] = []
for _b in range(256):
    _c = _b << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x1021) & 0xFFFF if _c & 0x8000 else (_c << 1) & 0xFFFF
    _TABLE.append(_c)


def crc16_ccitt_false(data: bytes | bytearray | memoryview, init: int = 0xFFFF) -> int:
    crc = init
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc
