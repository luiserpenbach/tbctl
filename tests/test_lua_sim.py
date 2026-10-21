"""Deterministic §6 state-machine tests against the mock's embedded Lua sim,
driven by a fake clock (1 ms virtual ticks)."""

import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.hw.registers_gen import ARM_MAGIC, REG_NAME, Cmd, SeqError, SeqState
from tbctl.seqtable import Step, StepTable
from tests.conftest import CONFIGS


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


@pytest.fixture
def chmap():
    return load_channels(CONFIGS / "channels.yaml").model


@pytest.fixture
def rig(chmap):
    clock = FakeClock()
    mock = MockDevice(chmap, clock=clock)
    mock.open()
    return mock, clock, chmap


def table(chmap, steps=None, abort=None, guard=2000) -> StepTable:
    safe = chmap.safe_word()
    return StepTable(
        dio_inhibit=chmap.dio_inhibit(),
        safe_word=safe,
        igniter_guard_ms=guard,
        igniter_mask=1 << 6,  # igniter.main on FIO6
        steps=tuple(steps or [Step(0, 0x0001), Step(50, 0x0003), Step(100, safe)]),
        abort=tuple(abort or [Step(0, safe), Step(20, safe)]),
    )


def upload_and_load(mock, t: StepTable):
    mock.write_name_array(REG_NAME["FIFO_DATA"], t.to_u16_words())
    mock.write_name(REG_NAME["CMD"], Cmd.LOAD)


def arm(mock, clock):
    mock.write_name(REG_NAME["HB_TIMEOUT_MS"], 500)
    mock.write_name(REG_NAME["HEARTBEAT"], 1)
    mock.write_name(REG_NAME["ARM_CODE"], ARM_MAGIC)
    mock.write_name(REG_NAME["CMD"], Cmd.ARM)


def state(mock) -> SeqState:
    return SeqState(int(mock.read_name(REG_NAME["STATE"])))


def error(mock) -> SeqError:
    return SeqError(int(mock.read_name(REG_NAME["ERROR_CODE"])))


def beat(mock, n=1):
    hb = int(mock._regs[REG_NAME["HEARTBEAT"]])
    mock.write_name(REG_NAME["HEARTBEAT"], (hb + n) & 0xFFFF)


def test_happy_path_load_arm_fire_done(rig):
    mock, clock, chmap = rig
    t = table(chmap)
    upload_and_load(mock, t)
    assert state(mock) == SeqState.LOADED
    assert int(mock.read_name(REG_NAME["CRC16"])) == t.crc16
    assert int(mock.read_name(REG_NAME["STEP_COUNT"])) == (3 << 8) | 2

    arm(mock, clock)
    assert state(mock) == SeqState.ARMED
    assert mock.dio_word16() == chmap.safe_word()  # defined start state

    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    clock.advance(0.010)
    beat(mock)
    assert state(mock) == SeqState.RUNNING
    assert mock.dio_word16() & 0x0001  # step 0 applied

    clock.advance(0.045)
    beat(mock)
    assert mock.dio_word16() & 0x0003 == 0x0003  # step at 50 ms applied

    clock.advance(0.060)
    beat(mock)
    assert state(mock) == SeqState.DONE
    assert mock.dio_word16() == chmap.safe_word()

    mock.write_name(REG_NAME["CMD"], Cmd.RESET)
    assert state(mock) == SeqState.IDLE


def test_catchup_applies_all_due_steps_in_order(rig):
    mock, clock, chmap = rig
    upload_and_load(mock, table(chmap))
    arm(mock, clock)
    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    # jump far past the end in one advance: every step must still apply
    clock.advance(0.5)
    beat(mock)
    assert state(mock) == SeqState.DONE
    assert mock.dio_word16() == chmap.safe_word()


def test_abort_mid_run_runs_abort_table(rig):
    mock, clock, chmap = rig
    upload_and_load(mock, table(chmap))
    arm(mock, clock)
    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    clock.advance(0.020)
    beat(mock)
    assert state(mock) == SeqState.RUNNING
    mock.write_name(REG_NAME["CMD"], Cmd.ABORT)
    assert state(mock) == SeqState.ABORTING
    assert error(mock) == SeqError.HOST_ABORT
    clock.advance(0.050)
    beat(mock)
    assert state(mock) == SeqState.ABORTED
    assert mock.dio_word16() == chmap.safe_word()


def test_heartbeat_loss_aborts(rig):
    mock, clock, chmap = rig
    upload_and_load(mock, table(chmap, steps=[Step(0, 0x0001), Step(5000, chmap.safe_word())]))
    arm(mock, clock)
    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    for _ in range(4):  # 400 ms with heartbeats: still running
        clock.advance(0.1)
        beat(mock)
    assert state(mock) == SeqState.RUNNING
    clock.advance(0.6)  # 600 ms silence > 500 ms timeout
    assert state(mock) in (SeqState.ABORTING, SeqState.ABORTED)
    assert error(mock) == SeqError.HB_TIMEOUT
    clock.advance(0.1)
    assert state(mock) == SeqState.ABORTED
    assert mock.dio_word16() == chmap.safe_word()


def test_heartbeat_loss_while_armed_aborts(rig):
    mock, clock, chmap = rig
    upload_and_load(mock, table(chmap))
    arm(mock, clock)
    assert state(mock) == SeqState.ARMED
    clock.advance(0.7)
    assert state(mock) == SeqState.ABORTED
    assert error(mock) == SeqError.HB_TIMEOUT


def test_igniter_guard_trips(rig):
    mock, clock, chmap = rig
    safe = chmap.safe_word()
    # igniter (bit 6) on at t=0 and never turned off before the 100 ms guard
    t = table(
        chmap,
        steps=[Step(0, 1 << 6), Step(5000, safe)],
        guard=100,
    )
    upload_and_load(mock, t)
    arm(mock, clock)
    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    clock.advance(0.050)
    beat(mock)
    assert state(mock) == SeqState.RUNNING
    assert mock.dio_word16() & (1 << 6)
    clock.advance(0.100)
    beat(mock)
    assert state(mock) in (SeqState.ABORTING, SeqState.ABORTED)
    assert error(mock) == SeqError.IGNITER_GUARD
    assert not mock.dio_word16() & (1 << 6)  # igniter forced low immediately
    clock.advance(0.1)
    beat(mock)
    assert state(mock) == SeqState.ABORTED


def test_truncated_upload_faults(rig):
    mock, _clock, chmap = rig
    words = table(chmap).to_u16_words()
    mock.write_name_array(REG_NAME["FIFO_DATA"], words[:-3])  # truncated
    mock.write_name(REG_NAME["CMD"], Cmd.LOAD)
    assert state(mock) == SeqState.FAULT
    assert error(mock) == SeqError.BAD_TABLE
    mock.write_name(REG_NAME["CMD"], Cmd.RESET)
    assert state(mock) == SeqState.IDLE


def test_corrupted_crc_faults(rig):
    mock, _clock, chmap = rig
    words = table(chmap).to_u16_words()
    words[8] ^= 0x0004  # flip a bit in a step
    mock.write_name_array(REG_NAME["FIFO_DATA"], words)
    mock.write_name(REG_NAME["CMD"], Cmd.LOAD)
    assert state(mock) == SeqState.FAULT
    assert error(mock) == SeqError.BAD_TABLE


def test_wrong_arm_code_rejected(rig):
    mock, _clock, chmap = rig
    upload_and_load(mock, table(chmap))
    mock.write_name(REG_NAME["ARM_CODE"], 0x1234)
    mock.write_name(REG_NAME["CMD"], Cmd.ARM)
    assert state(mock) == SeqState.LOADED  # rejected, no transition
    # FIRE without ARM also rejected
    mock.write_name(REG_NAME["CMD"], Cmd.FIRE)
    assert state(mock) == SeqState.LOADED


def test_abort_while_armed_goes_safe(rig):
    mock, clock, chmap = rig
    upload_and_load(mock, table(chmap))
    arm(mock, clock)
    mock.write_name(REG_NAME["CMD"], Cmd.ABORT)
    assert state(mock) == SeqState.ABORTED
    assert error(mock) == SeqError.HOST_ABORT
    assert mock.dio_word16() == chmap.safe_word()


def test_load_rejected_while_not_idle(rig):
    mock, _clock, chmap = rig
    upload_and_load(mock, table(chmap))
    assert state(mock) == SeqState.LOADED
    # a second LOAD without RESET is ignored (fifo drains only on LOAD in IDLE)
    mock.write_name_array(REG_NAME["FIFO_DATA"], table(chmap).to_u16_words())
    mock.write_name(REG_NAME["CMD"], Cmd.LOAD)
    assert state(mock) == SeqState.LOADED
