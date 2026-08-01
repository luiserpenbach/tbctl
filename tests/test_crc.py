"""CRC-16/CCITT-FALSE golden vectors (spec D8, §10.1) — Python side + Lua harness."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tbctl.crc import crc16_ccitt_false

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS = REPO_ROOT / "tests" / "golden" / "crc_vectors.txt"


def load_vectors() -> list[tuple[bytes, int]]:
    out = []
    for line in VECTORS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        hex_bytes, expect = line.split()
        data = b"" if hex_bytes == "-" else bytes.fromhex(hex_bytes)
        out.append((data, int(expect, 16)))
    return out


def test_vectors_file_nonempty():
    assert len(load_vectors()) >= 5


def test_published_check_value():
    # External anchor: the published CRC-16/CCITT-FALSE check value.
    assert crc16_ccitt_false(b"123456789") == 0x29B1


def test_init_value():
    assert crc16_ccitt_false(b"") == 0xFFFF


@pytest.mark.parametrize("data,expected", load_vectors())
def test_golden_vectors_python(data: bytes, expected: int):
    assert crc16_ccitt_false(data) == expected


def test_incremental_equals_oneshot():
    data = bytes(range(200))
    running = 0xFFFF
    for chunk in (data[:7], data[7:100], data[100:]):
        running = crc16_ccitt_false(chunk, init=running)
    assert running == crc16_ccitt_false(data)


def _find_lua() -> str | None:
    for name in ("lua5.4", "lua5.3", "lua"):
        if path := shutil.which(name):
            return path
    return None


@pytest.mark.skipif(_find_lua() is None, reason="no lua interpreter installed")
def test_golden_vectors_lua():
    """Cross-check: the Lua implementation must pass the same vectors (D8)."""
    result = subprocess.run(
        [_find_lua(), "tests/lua/run_crc_tests.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"lua harness failed:\n{result.stdout}{result.stderr}"
