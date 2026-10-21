"""§5 step-table wire format: host <-> T7 FIFO binary encoding.

Little-endian, all integers unsigned:

    header (12 bytes):
      n_steps        u16
      n_abort_steps  u16
      dio_inhibit    u16   # bits NOT controlled by the sequencer
      safe_word      u16   # fail-safe output image
      igniter_guard_ms u16   # D9; 0 = no igniter bit present
      igniter_mask     u16   # DIO bit(s) of the igniter line the guard watches
    steps:      n_steps       x (t_ms u32, dio_word u16)
    abort:      n_abort_steps x (t_ms u32, dio_word u16)
    trailer (2 bytes):
      crc16          u16   # CRC-16/CCITT-FALSE over header+steps+abort

The igniter_mask field is a deliberate extension of the original hand-off §5
layout (10-byte header): the Lua guard must know which bit is the igniter
line, and that assignment has to sit inside the CRC-protected table.

The Lua interpreter re-parses these bytes independently and recomputes the
CRC over what it parsed; the host compares read-back CRC and step counts —
that comparison IS the load verification.
"""

from __future__ import annotations

import itertools
import struct
from dataclasses import dataclass, field

from tbctl.crc import crc16_ccitt_false
from tbctl.hw.registers_gen import HEADER_BYTES, MAX_STEPS, STEP_BYTES, TRAILER_BYTES

_HEADER = struct.Struct("<HHHHHH")
_STEP = struct.Struct("<IH")
_CRC = struct.Struct("<H")

assert _HEADER.size == HEADER_BYTES
assert _STEP.size == STEP_BYTES
assert _CRC.size == TRAILER_BYTES


class TableFormatError(Exception):
    """Malformed step-table payload."""


@dataclass(frozen=True)
class Step:
    t_ms: int  # u32 offset from FIRE (main) or abort start (abort)
    dio_word: int  # u16 full output state

    def __post_init__(self) -> None:
        if not 0 <= self.t_ms <= 0xFFFFFFFF:
            raise TableFormatError(f"t_ms {self.t_ms} out of u32 range")
        if not 0 <= self.dio_word <= 0xFFFF:
            raise TableFormatError(f"dio_word {self.dio_word:#x} out of u16 range")


@dataclass(frozen=True)
class StepTable:
    dio_inhibit: int
    safe_word: int
    igniter_guard_ms: int
    igniter_mask: int
    steps: tuple[Step, ...] = field(default_factory=tuple)
    abort: tuple[Step, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        for name, val in (
            ("dio_inhibit", self.dio_inhibit),
            ("safe_word", self.safe_word),
            ("igniter_guard_ms", self.igniter_guard_ms),
            ("igniter_mask", self.igniter_mask),
        ):
            if not 0 <= val <= 0xFFFF:
                raise TableFormatError(f"{name} {val:#x} out of u16 range")
        for what, steps in (("steps", self.steps), ("abort", self.abort)):
            if len(steps) > MAX_STEPS:
                raise TableFormatError(f"{what}: {len(steps)} exceeds {MAX_STEPS}")
            for prev, cur in itertools.pairwise(steps):
                if cur.t_ms < prev.t_ms:
                    raise TableFormatError(f"{what}: step times not monotonic")
        if self.igniter_guard_ms and not self.igniter_mask:
            raise TableFormatError("igniter_guard_ms set but igniter_mask empty")

    # -- encoding ----------------------------------------------------------

    def payload(self) -> bytes:
        """Header + steps + abort (the CRC-covered region)."""
        parts = [
            _HEADER.pack(
                len(self.steps),
                len(self.abort),
                self.dio_inhibit,
                self.safe_word,
                self.igniter_guard_ms,
                self.igniter_mask,
            )
        ]
        parts += [_STEP.pack(s.t_ms, s.dio_word) for s in (*self.steps, *self.abort)]
        return b"".join(parts)

    @property
    def crc16(self) -> int:
        return crc16_ccitt_false(self.payload())

    def pack(self) -> bytes:
        """Full wire image including CRC trailer."""
        payload = self.payload()
        return payload + _CRC.pack(crc16_ccitt_false(payload))

    def to_u16_words(self) -> list[int]:
        """Wire image as little-endian u16 words for the FIFO data register."""
        raw = self.pack()
        assert len(raw) % 2 == 0
        return [int.from_bytes(raw[i : i + 2], "little") for i in range(0, len(raw), 2)]

    # -- decoding ----------------------------------------------------------

    @classmethod
    def unpack(cls, raw: bytes) -> StepTable:
        """Parse + CRC-verify a full wire image. Raises TableFormatError."""
        if len(raw) < HEADER_BYTES + TRAILER_BYTES:
            raise TableFormatError(f"truncated: {len(raw)} bytes")
        n_steps, n_abort, inhibit, safe, guard, mask = _HEADER.unpack_from(raw, 0)
        expect = HEADER_BYTES + (n_steps + n_abort) * STEP_BYTES + TRAILER_BYTES
        if len(raw) != expect:
            raise TableFormatError(f"length {len(raw)} != expected {expect}")
        (crc_stated,) = _CRC.unpack_from(raw, len(raw) - TRAILER_BYTES)
        crc_actual = crc16_ccitt_false(raw[: len(raw) - TRAILER_BYTES])
        if crc_stated != crc_actual:
            raise TableFormatError(
                f"CRC mismatch: stated {crc_stated:#06x}, actual {crc_actual:#06x}"
            )

        def read_steps(offset: int, count: int) -> tuple[Step, ...]:
            out = []
            for i in range(count):
                t_ms, word = _STEP.unpack_from(raw, offset + i * STEP_BYTES)
                out.append(Step(t_ms, word))
            return tuple(out)

        steps = read_steps(HEADER_BYTES, n_steps)
        abort = read_steps(HEADER_BYTES + n_steps * STEP_BYTES, n_abort)
        return cls(inhibit, safe, guard, mask, steps, abort)
