"""Compiler unit tests + §10.2 service-level integration tests on the sim."""

import asyncio

import pytest

from tbctl.config.loader import load_channels, load_config_dir
from tbctl.config.models import Sequence
from tbctl.hw.registers_gen import SeqError, SeqState
from tbctl.services.compiler import CompileError, compile_sequence
from tests.conftest import CONFIGS


@pytest.fixture
def chmap():
    return load_channels(CONFIGS / "channels.yaml").model


def seq(steps, abort=None, **overrides) -> Sequence:
    data = {
        "name": "t",
        "max_duration_s": 30.0,
        "steps": steps,
        "abort": abort
        or [
            {
                "t": 0.0,
                "set": {
                    "valve.ox_main": "close",
                    "valve.fuel_main": "close",
                    "spark.igniter": "off",
                },
            }
        ],
    }
    data.update(overrides)
    return Sequence.model_validate(data)


# ------------------------------------------------------------------ compiler


def test_compile_hotfire_example(chmap):
    bundle = load_config_dir(CONFIGS)
    compiled = compile_sequence(bundle.sequences["hotfire_v1"].model, chmap)
    r = compiled.report()
    assert r["n_steps"] == 10
    assert r["spark_guard_ms"] == 2000
    assert r["spark_mask"] == 1 << 6
    assert r["warnings"] == []
    # carry-forward: at t=2.60 purge closed, spark on, ox+fuel open
    words = {s.t_ms: s.dio_word for s in compiled.table.steps}
    assert words[2600] == (1 << 6) | 0b11  # spark + ox_main + fuel_main
    assert words[2800] == 0b11  # spark off, mains still open
    assert compiled.table.steps[-1].dio_word == chmap.safe_word()


def test_carry_forward_from_safe_word(chmap):
    compiled = compile_sequence(
        seq(
            [
                {"t": 0.0, "set": {"valve.ox_vent": "close"}},  # NC vent: close = energize
                {"t": 1.0, "set": {"valve.ox_vent": "open"}},
            ]
        ),
        chmap,
    )
    safe = chmap.safe_word()
    # ox_vent open_state=low, bit 2: close -> level 1
    assert compiled.table.steps[0].dio_word == safe | (1 << 2)
    assert compiled.table.steps[1].dio_word == safe


def test_unsafe_end_rejected(chmap):
    with pytest.raises(CompileError, match="allow_unsafe_end"):
        compile_sequence(seq([{"t": 0.0, "set": {"valve.ox_main": "open"}}]), chmap)


def test_unsafe_end_allowed_with_flag_warns(chmap):
    compiled = compile_sequence(
        seq([{"t": 0.0, "set": {"valve.ox_main": "open"}}], allow_unsafe_end=True), chmap
    )
    assert compiled.warnings and "allow_unsafe_end" in compiled.warnings[0]


def test_abort_must_end_safe(chmap):
    with pytest.raises(CompileError, match="abort table ends"):
        compile_sequence(
            seq(
                [
                    {"t": 0.0, "set": {"valve.ox_main": "open"}},
                    {"t": 1.0, "set": {"valve.ox_main": "close"}},
                ],
                abort=[{"t": 0.0, "set": {"valve.purge": "open"}}],
            ),
            chmap,
        )


def test_static_spark_on_time_rejected(chmap):
    with pytest.raises(CompileError, match="spark on for"):
        compile_sequence(
            seq(
                [
                    {"t": 0.0, "set": {"spark.igniter": "on"}},
                    {"t": 3.0, "set": {"spark.igniter": "off"}},  # 3000 ms > 2000 max_on_ms
                ]
            ),
            chmap,
        )


def test_spark_on_at_table_end_counts(chmap):
    with pytest.raises(CompileError):  # unsafe end AND implied on-time to end
        compile_sequence(seq([{"t": 0.0, "set": {"spark.igniter": "on"}}]), chmap)


def test_unknown_tag_rejected(chmap):
    with pytest.raises(CompileError, match="not an output"):
        compile_sequence(seq([{"t": 0.0, "set": {"valve.nope": "open"}}]), chmap)


# ------------------------------------------------- service integration (sim)


@pytest.fixture
async def node(tmp_path):
    from tbctl.node import build_context

    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    # fast heartbeat timeout so loss tests run quickly
    ctx.sequencer._hb_timeout_ms = 150
    ctx.iface.open()
    await ctx.acq.start()
    yield ctx
    await ctx.sequencer.shutdown()
    await ctx.acq.stop()
    ctx.iface.close()


def _write_test_sequence(ctx, name="quick", steps=None):
    """Inject a fast sequence into the bundle for timing-friendly tests."""
    import hashlib

    from tbctl.config.loader import Loaded

    model = Sequence.model_validate(
        {
            "name": name,
            "max_duration_s": 5.0,
            "preconditions": [{"tag": "pressure.n2_purge", "min": 5.0}],
            "steps": steps
            or [
                {"t": 0.0, "set": {"valve.purge": "open"}},
                {"t": 0.15, "set": {"valve.ox_main": "open"}},
                {"t": 0.3, "set": {"valve.ox_main": "close"}},
                {"t": 0.35, "set": {"valve.purge": "close"}},
            ],
            "abort": [
                {"t": 0.0, "set": {"valve.ox_main": "close"}},
                {"t": 0.05, "set": {"valve.purge": "close"}},
            ],
        }
    )
    raw = repr(model).encode()
    ctx.bundle.sequences[name] = Loaded(model, hashlib.sha256(raw).hexdigest(), CONFIGS / "x.yaml")
    return model


async def _await_state(ctx, want, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while True:
        st = ctx.sequencer.state()
        if st == want:
            return
        assert asyncio.get_event_loop().time() < end, f"timeout waiting {want.name}, at {st.name}"
        await asyncio.sleep(0.02)


async def test_happy_path_end_to_end(node):
    ctx = node
    _write_test_sequence(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)  # let live data accumulate for preconditions

    report = await ctx.sequencer.load("quick")
    assert report["crc_match"] is True
    assert ctx.sequencer.state() == SeqState.LOADED

    await ctx.sequencer.arm("quick", control_enabled=True)
    assert ctx.sequencer.state() == SeqState.ARMED

    await ctx.sequencer.fire("quick")
    await _await_state(ctx, SeqState.DONE)
    assert ctx.iface.dio_word16() == ctx.bundle.channels.model.safe_word()

    await ctx.sequencer.reset()
    assert ctx.sequencer.state() == SeqState.IDLE


async def test_precondition_gate(node):
    ctx = node
    _write_test_sequence(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 1.0, noise_sigma_eu=0.0)  # below min 5.0
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    from tbctl.services.sequencer import SequencerError

    with pytest.raises(SequencerError, match="preconditions failed"):
        await ctx.sequencer.arm("quick", control_enabled=True)


async def test_arm_requires_control_and_confirm(node):
    ctx = node
    _write_test_sequence(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    from tbctl.services.sequencer import SequencerError

    with pytest.raises(SequencerError, match="control mode"):
        await ctx.sequencer.arm("quick", control_enabled=False)
    with pytest.raises(SequencerError, match="confirm mismatch"):
        await ctx.sequencer.arm("wrong_name", control_enabled=True)


async def test_abort_mid_run(node):
    ctx = node
    _write_test_sequence(
        ctx,
        steps=[
            {"t": 0.0, "set": {"valve.ox_main": "open"}},
            {"t": 2.0, "set": {"valve.ox_main": "close"}},
        ],
    )
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    await ctx.sequencer.arm("quick", control_enabled=True)
    await ctx.sequencer.fire("quick")
    await asyncio.sleep(0.1)
    await ctx.sequencer.abort()
    await _await_state(ctx, SeqState.ABORTED)
    assert ctx.sequencer.error() == SeqError.HOST_ABORT
    assert ctx.iface.dio_word16() == ctx.bundle.channels.model.safe_word()


async def test_heartbeat_loss_aborts_autonomously(node):
    ctx = node
    _write_test_sequence(
        ctx,
        steps=[
            {"t": 0.0, "set": {"valve.ox_main": "open"}},
            {"t": 3.0, "set": {"valve.ox_main": "close"}},
        ],
    )
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    await ctx.sequencer.arm("quick", control_enabled=True)
    await ctx.sequencer.fire("quick")
    # kill the host heartbeat: the "Lua" must abort on its own
    await ctx.sequencer._heartbeat.stop()
    await asyncio.sleep(0.4)  # > 150 ms timeout
    await _await_state(ctx, SeqState.ABORTED)
    assert ctx.sequencer.error() == SeqError.HB_TIMEOUT


async def test_manual_lockout_while_not_idle(node):
    ctx = node
    _write_test_sequence(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)

    from tbctl.services.manual_control import ManualControlError

    # control disabled -> rejected
    with pytest.raises(ManualControlError, match="control mode"):
        ctx.manual.set_output("valve.purge", "open", control_enabled=False)
    # IDLE + enabled -> works
    ctx.manual.set_output("valve.purge", "open", control_enabled=True)
    assert ctx.iface.read_name("FIO4") == 1.0
    ctx.manual.set_output("valve.purge", "close", control_enabled=True)

    await ctx.sequencer.load("quick")
    await ctx.sequencer.arm("quick", control_enabled=True)
    with pytest.raises(ManualControlError, match="locked out"):
        ctx.manual.set_output("valve.purge", "open", control_enabled=True)
    await ctx.sequencer.abort()
    await _await_state(ctx, SeqState.ABORTED)


async def test_verify_catches_wrong_crc(node):
    """Load verification compares device CRC against host CRC."""
    ctx = node
    _write_test_sequence(ctx)
    # sabotage: corrupt one FIFO word mid-upload by wrapping write_name_array
    orig = ctx.iface.write_name_array

    def corrupt(name, values):
        vals = list(values)
        vals[10] ^= 0x0001
        orig(name, vals)

    ctx.iface.write_name_array = corrupt
    from tbctl.services.sequencer import SequencerError

    with pytest.raises(SequencerError, match="rejected table|verification failed"):
        await ctx.sequencer.load("quick")
    ctx.iface.write_name_array = orig
