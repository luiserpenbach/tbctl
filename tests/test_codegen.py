"""Codegen: symbolic register resolution against ljm_constants.json (spec D10)."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "shared"))

import codegen  # noqa: E402


@pytest.fixture(scope="module")
def ljm() -> codegen.LjmConstants:
    return codegen.LjmConstants(codegen.VENDORED_CONSTANTS)


def test_user_ram_u16_expansion(ljm):
    # USER_RAM#(0:19)_U16 base 46180, one word each
    assert ljm.resolve("USER_RAM0_U16").address == 46180
    assert ljm.resolve("USER_RAM9_U16").address == 46189
    assert ljm.resolve("USER_RAM19_U16").address == 46199


def test_user_ram_u32_expansion(ljm):
    # USER_RAM#(0:39)_U32 base 46100, two words each
    assert ljm.resolve("USER_RAM0_U32").address == 46100
    assert ljm.resolve("USER_RAM3_U32").address == 46106


def test_fifo_and_dio_registers(ljm):
    assert ljm.resolve("USER_RAM_FIFO0_DATA_U16").address == 47000
    assert ljm.resolve("DIO_STATE").address == 2800
    assert ljm.resolve("DIO_INHIBIT").address == 2900


def test_out_of_range_index_rejected(ljm):
    with pytest.raises(KeyError):
        ljm.resolve("USER_RAM20_U16")  # range is 0:19
    with pytest.raises(KeyError):
        ljm.resolve("TOTALLY_FAKE_REGISTER")


def test_full_resolution_no_overlaps(ljm):
    spec = codegen.load_spec()
    resolved = codegen.resolve_all(spec, ljm)  # raises on overlap
    assert resolved["CMD"].address != resolved["STATE"].address


def test_generated_files_in_sync():
    """The checked-in generated files must match a fresh codegen run."""
    assert codegen.generate(check=True) == 0


def test_spec_validation_current_spec_passes():
    codegen.validate_spec(codegen.load_spec())


def test_spec_validation_rejects_nonunit_tick_ms():
    """F4: the Lua compares ms limits directly to tick counts; tick_ms must be 1."""
    spec = codegen.load_spec()
    spec["constants"]["tick_ms"] = 2
    with pytest.raises(ValueError, match="tick_ms"):
        codegen.validate_spec(spec)


def test_spec_validation_rejects_fifo_smaller_than_max_table():
    """F2: raising max_steps past FIFO capacity must fail codegen."""
    spec = codegen.load_spec()
    spec["constants"]["max_steps"] = 256  # 12 + 2*256*6 + 2 = 3086 B > 2048 B
    with pytest.raises(ValueError, match="allocate_bytes"):
        codegen.validate_spec(spec)
    spec = codegen.load_spec()
    spec["fifo"]["table_upload"]["allocate_bytes"] = 1549  # one byte short of 1550
    with pytest.raises(ValueError, match="allocate_bytes"):
        codegen.validate_spec(spec)


def test_spec_validation_rejects_slot_without_lua_key():
    """F1: adding a registers.yaml slot with no Lua counterpart must fail codegen."""
    spec = codegen.load_spec()
    spec["slots"]["host_to_lua"]["NEW_SLOT"] = {"slot": "USER_RAM10_U16", "desc": "x"}
    with pytest.raises(ValueError, match="NEW_SLOT"):
        codegen.validate_spec(spec)


def test_spec_validation_rejects_constant_without_lua_key():
    spec = codegen.load_spec()
    spec["constants"]["new_limit_ms"] = 5
    with pytest.raises(ValueError, match="NEW_LIMIT_MS"):
        codegen.validate_spec(spec)


def test_spec_validation_rejects_stale_lua_keys():
    """F1 reverse direction: a Lua key whose spec entry was removed must fail."""
    spec = codegen.load_spec()
    del spec["slots"]["host_to_lua"]["HB_TIMEOUT_MS"]
    with pytest.raises(ValueError, match="HB_TIMEOUT_MS"):
        codegen.validate_spec(spec)


def test_generated_python_importable():
    from tbctl.hw import registers_gen as rg

    assert rg.ARM_MAGIC == 0xA55A
    assert rg.Cmd.ABORT == 4
    assert rg.SeqState.FAULT == 7
    assert rg.SeqError.SPARK_GUARD == 4
    assert rg.REG_NAME["CMD"] == "USER_RAM0_U16"
    assert rg.REG_ADDR["ELAPSED_MS"] == 46100
    assert rg.INTERP_VERSION == 1
