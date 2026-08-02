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
    assert "function U.cb" in src  # incremental per-byte CRC (streaming LOAD)
    assert "U.b={}" not in src  # no byte buffer — it OOMed the T7 heap
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


@pytest.mark.skipif(_find_lua() is None, reason="no lua interpreter installed")
def test_minified_sequencer_state_machine(tmp_path):
    """T3: the DEPLOYED form (minify_lua output) must pass the full harness."""
    from tbctl.hw.script_loader import minify_lua

    source = (REPO_ROOT / "lua" / "sequencer.lua").read_text(encoding="utf-8")
    minified = minify_lua(source)
    assert minified != source, "minifier did nothing -- deployed artifact untested"
    assert "--" not in minified.replace("- -", "")  # comments stripped, no accidental `--`
    target = tmp_path / "sequencer.min.lua"
    target.write_text(minified, encoding="utf-8")
    result = subprocess.run(
        [_find_lua(), "tests/lua/run_sequencer_tests.lua", str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"minified lua harness failed:\n{result.stdout}{result.stderr}"
    assert "checks OK" in result.stdout


def test_minify_lua_preserves_strings_comment_markers_and_unary_minus():
    """F3: the minifier must never corrupt strings, `- -` sequences, or code."""
    from tbctl.hw.script_loader import minify_lua

    src = (
        'local s = "a -- not a comment"  -- real comment\n'
        "local t = 'x--y'\n"
        "  local n = a - -b  \n"
        "-- full-line comment\n"
        'local esc = "quote \\" then -- still string"\n'
    )
    out = minify_lua(src)
    assert '"a -- not a comment"' in out
    assert "'x--y'" in out
    assert "a - -b" in out  # internal whitespace untouched: no `--` created
    assert '"quote \\" then -- still string"' in out
    assert "real comment" not in out
    assert "full-line comment" not in out


def test_minify_lua_bails_on_long_brackets():
    """F3: block comments / long strings are beyond the line-based pass."""
    from tbctl.hw.script_loader import minify_lua

    for src in (
        "local s = [[long\nstring]]\n",
        "--[[ block\ncomment ]]\nlocal x = 1\n",
        "local s = [==[deep]==]\n",
    ):
        assert minify_lua(src) == src
