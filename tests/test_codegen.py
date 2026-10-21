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


def test_generated_python_importable():
    from tbctl.hw import registers_gen as rg

    assert rg.ARM_MAGIC == 0xA55A
    assert rg.Cmd.ABORT == 4
    assert rg.SeqState.FAULT == 7
    assert rg.SeqError.IGNITER_GUARD == 4
    assert rg.REG_NAME["CMD"] == "USER_RAM0_U16"
    assert rg.REG_ADDR["ELAPSED_MS"] == 46100
    assert rg.INTERP_VERSION == 1
