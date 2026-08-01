"""§5 wire format: pack/unpack round-trip and malformed-payload rejection."""

import pytest

from tbctl.crc import crc16_ccitt_false
from tbctl.seqtable import Step, StepTable, TableFormatError


def table() -> StepTable:
    return StepTable(
        dio_inhibit=0xFF80,
        safe_word=0x0000,
        spark_guard_ms=2000,
        spark_mask=0x0040,
        steps=(Step(0, 0x0010), Step(2000, 0x0000), Step(2500, 0x0041)),
        abort=(Step(0, 0x0000), Step(100, 0x0010)),
    )


def test_roundtrip():
    t = table()
    raw = t.pack()
    assert len(raw) == 12 + 5 * 6 + 2
    assert StepTable.unpack(raw) == t


def test_crc_is_ccitt_false_over_payload():
    t = table()
    raw = t.pack()
    assert int.from_bytes(raw[-2:], "little") == crc16_ccitt_false(raw[:-2]) == t.crc16


def test_u16_words_little_endian():
    words = table().to_u16_words()
    raw = table().pack()
    assert len(words) == len(raw) // 2
    assert words[0] == 3  # n_steps
    assert words[1] == 2  # n_abort_steps


def test_corrupted_byte_rejected():
    raw = bytearray(table().pack())
    raw[15] ^= 0x01
    with pytest.raises(TableFormatError, match="CRC mismatch"):
        StepTable.unpack(bytes(raw))


def test_truncated_rejected():
    raw = table().pack()
    with pytest.raises(TableFormatError):
        StepTable.unpack(raw[:-4])


def test_non_monotonic_rejected():
    with pytest.raises(TableFormatError, match="monotonic"):
        StepTable(0, 0, 0, 0, steps=(Step(100, 0), Step(50, 0)))


def test_guard_without_mask_rejected():
    with pytest.raises(TableFormatError, match="spark_mask"):
        StepTable(0, 0, spark_guard_ms=100, spark_mask=0)


def test_too_many_steps_rejected():
    steps = tuple(Step(i * 2, 0) for i in range(129))
    with pytest.raises(TableFormatError, match="exceeds"):
        StepTable(0, 0, 0, 0, steps=steps)


def test_max_size_fits_fifo():
    steps = tuple(Step(i * 2, 0) for i in range(128))
    t = StepTable(0, 0, 0, 0, steps=steps, abort=steps)
    assert len(t.pack()) <= 2048  # FIFO allocation
