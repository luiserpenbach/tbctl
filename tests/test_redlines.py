"""Redline watchers: schema, debounce, arm/disarm lifecycle, auto-abort on sim."""

import asyncio
import hashlib

import pytest
from pydantic import ValidationError

from tbctl.config.loader import Loaded, load_channels
from tbctl.config.models import Redline, Sequence, cross_validate_sequence
from tbctl.hw.registers_gen import SeqError, SeqState
from tests.conftest import CONFIGS

# ------------------------------------------------------------------- schema


def test_redline_needs_bound():
    with pytest.raises(ValidationError, match="min and/or max"):
        Redline.model_validate({"tag": "pressure.chamber"})
    with pytest.raises(ValidationError, match="min must be"):
        Redline.model_validate({"tag": "a.b", "min": 5.0, "max": 1.0})
    r = Redline.model_validate({"tag": "a.b", "max": 22.0})
    assert r.persist_ms == 50


def test_redline_tag_must_be_input():
    chmap = load_channels(CONFIGS / "channels.yaml").model
    seq = Sequence.model_validate(
        {
            "name": "s",
            "max_duration_s": 5.0,
            "steps": [{"t": 0.0, "set": {"valve.purge": "open"}}],
            "abort": [{"t": 0.0, "set": {"valve.purge": "close"}}],
            "redlines": [{"tag": "valve.ox_main", "max": 1.0}],
        }
    )
    errors = cross_validate_sequence(seq, chmap)
    assert any("redlines[0]" in e for e in errors)


def test_hotfire_example_has_redlines():
    from tbctl.config.loader import load_config_dir

    bundle = load_config_dir(CONFIGS)
    rls = bundle.sequences["hotfire_v1"].model.redlines
    assert any(r.tag == "pressure.chamber" and r.max == 22.0 for r in rls)


# -------------------------------------------------------- integration (sim)


@pytest.fixture
async def node(tmp_path):
    from tbctl.node import build_context

    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    ctx.iface.open()
    await ctx.acq.start()
    yield ctx
    await ctx.sequencer.shutdown()
    await ctx.acq.stop()
    ctx.iface.close()


def _inject_sequence(ctx, redlines):
    model = Sequence.model_validate(
        {
            "name": "rl_test",
            "max_duration_s": 10.0,
            "steps": [
                {"t": 0.0, "set": {"valve.ox_main": "open"}},
                {"t": 6.0, "set": {"valve.ox_main": "close"}},
            ],
            "abort": [{"t": 0.0, "set": {"valve.ox_main": "close"}}],
            "redlines": redlines,
        }
    )
    raw = repr(model).encode()
    ctx.bundle.sequences["rl_test"] = Loaded(
        model, hashlib.sha256(raw).hexdigest(), CONFIGS / "x.yaml"
    )


async def _await_state(ctx, want, timeout=5.0):
    end = asyncio.get_event_loop().time() + timeout
    while ctx.sequencer.state() != want:
        assert asyncio.get_event_loop().time() < end, f"stuck at {ctx.sequencer.state().name}"
        await asyncio.sleep(0.02)


async def test_redline_trips_and_aborts(node):
    ctx = node
    _inject_sequence(ctx, [{"tag": "pressure.chamber", "max": 15.0, "persist_ms": 40}])
    ctx.iface.sim_set("pressure.chamber", 5.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)

    await ctx.sequencer.load("rl_test")
    await ctx.sequencer.arm("rl_test", control_enabled=True)
    assert ctx.sequencer.redlines.armed
    await ctx.sequencer.fire("rl_test", control_enabled=True)
    await asyncio.sleep(0.3)
    assert ctx.sequencer.state() == SeqState.RUNNING

    # drive chamber pressure over the redline
    ctx.iface.sim_set("pressure.chamber", 30.0, noise_sigma_eu=0.0)
    await _await_state(ctx, SeqState.ABORTED, timeout=3.0)
    assert ctx.sequencer.error() == SeqError.HOST_ABORT
    assert ctx.iface.dio_word16() == ctx.bundle.channels.model.safe_word()
    # event recorded with limit details
    events = ctx.events.tail(20)
    trip = next(e for e in events if e["event"] == "redline_tripped")
    assert trip["tag"] == "pressure.chamber" and "max 15.0" in trip["limit"]
    # watcher disarms via the terminal watch
    await ctx.sequencer.watch_terminal()
    assert not ctx.sequencer.redlines.armed


async def test_redline_debounce_ignores_short_spike(node):
    ctx = node
    # persist_ms much longer than one 100 ms spike
    _inject_sequence(ctx, [{"tag": "pressure.chamber", "max": 15.0, "persist_ms": 800}])
    ctx.iface.sim_set("pressure.chamber", 5.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("rl_test")
    await ctx.sequencer.arm("rl_test", control_enabled=True)
    await ctx.sequencer.fire("rl_test", control_enabled=True)
    await asyncio.sleep(0.2)

    ctx.iface.sim_set("pressure.chamber", 30.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.15)  # brief spike, shorter than persist_ms
    ctx.iface.sim_set("pressure.chamber", 5.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.5)
    assert ctx.sequencer.state() == SeqState.RUNNING  # survived the spike
    await ctx.sequencer.abort()
    await _await_state(ctx, SeqState.ABORTED)


async def test_no_redlines_means_no_watch(node):
    ctx = node
    _inject_sequence(ctx, [])
    ctx.iface.sim_set("pressure.chamber", 5.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("rl_test")
    await ctx.sequencer.arm("rl_test", control_enabled=True)
    assert not ctx.sequencer.redlines.armed
    await ctx.sequencer.abort()
    await _await_state(ctx, SeqState.ABORTED)
