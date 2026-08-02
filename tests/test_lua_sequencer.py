"""Runs the REAL assembled lua/sequencer.lua under desktop Lua (§10.1)."""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_lua() -> str | None:
    for name in ("lua5.4", "lua5.3", "lua"):
        if path := shutil.which(name):
            return path
    return None


def test_sequencer_artifact_exists_and_is_assembled():
    src = (REPO_ROOT / "lua" / "sequencer.lua").read_text()
    assert "ASSEMBLED ARTIFACT" in src
    assert "DO NOT EDIT LOGIC WITHOUT BUMPING INTERP_VERSION" in src
    assert "U.A = {" in src  # generated header inlined (single-upvalue table)
    assert "function U.crc16" in src  # CRC inlined into U
    assert "return {" not in src  # module export stripped


@pytest.mark.skipif(_find_lua() is None, reason="no lua interpreter installed")
def test_real_sequencer_state_machine():
    result = subprocess.run(
        [_find_lua(), "tests/lua/run_sequencer_tests.lua"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"lua harness failed:\n{result.stdout}{result.stderr}"
    assert "checks OK" in result.stdout
