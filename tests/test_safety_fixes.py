"""Regression tests for the safety-hardening fixes (bug-hunt 2026-08-02).

Covers the backend fixes that previously had zero coverage: the operator abort
HTTP path (T1), control-mode gating of FIRE and auto-abort on disable (H12),
clean RESET from ARMED (M6), NaN/stale precondition and redline handling (C6/M5),
the out-of-band safe-output escalation (C2), LJM dummy-sample masking (H14), and
the recorder always being stoppable even when a flush fails (H2).
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from tbctl.config.loader import Loaded
from tbctl.config.models import Sequence
from tbctl.hw.registers_gen import SeqError, SeqState
from tests.conftest import CONFIGS


def _inject_quick(ctx, name="quick", steps=None):
    model = Sequence.model_validate(
        {
            "name": name,
            "max_duration_s": 5.0,
            "preconditions": [{"tag": "pressure.n2_purge", "min": 5.0}],
            "steps": steps
            or [
                {"t": 0.0, "set": {"valve.purge": "open"}},
                {"t": 0.5, "set": {"valve.ox_main": "open"}},
                {"t": 1.5, "set": {"valve.ox_main": "close"}},
                {"t": 1.6, "set": {"valve.purge": "close"}},
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


# --------------------------------------------------------- service-level fixture


@pytest.fixture
async def node(tmp_path):
    from tbctl.node import build_context

    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    ctx.sequencer._hb_timeout_ms = 300
    ctx.iface.open()
    await ctx.acq.start()
    yield ctx
    await ctx.sequencer.shutdown()
    await ctx.acq.stop()
    ctx.iface.close()


# ---------------------------------------------------------------- API fixture


@pytest.fixture
async def client_ctx(tmp_path):
    import httpx

    from tbctl.node import build_app, build_context

    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    ctx.sequencer._hb_timeout_ms = 400
    app = build_app(ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield ctx, client


async def _await_state(ctx, want, timeout=5.0):
    import asyncio

    end = asyncio.get_event_loop().time() + timeout
    while ctx.sequencer.state() != want:
        assert asyncio.get_event_loop().time() < end, f"timeout waiting for {want.name}"
        await asyncio.sleep(0.02)


# ============================================================ T1: abort endpoint


async def test_abort_endpoint_confirms_and_safes(client_ctx):
    """POST /api/sequences/abort actually aborts and reports the verified outcome."""
    import asyncio

    ctx, client = client_ctx
    _inject_quick(
        ctx,
        steps=[
            {"t": 0.0, "set": {"valve.ox_main": "open"}},
            {"t": 2.0, "set": {"valve.ox_main": "close"}},
        ],
    )
    await client.post("/api/control-mode", json={"enabled": True})
    await client.post("/api/sim/set", json={"tag": "pressure.n2_purge", "value": 9.0})
    await asyncio.sleep(0.3)
    assert (await client.post("/api/sequences/quick/load")).status_code == 200
    assert (await client.post("/api/sequences/arm", json={"confirm": "quick"})).status_code == 200
    assert (await client.post("/api/sequences/fire", json={"confirm": "quick"})).status_code == 200
    await asyncio.sleep(0.1)

    r = await client.post("/api/sequences/abort")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["confirmed"] is True
    assert body["escalated"] is False
    await _await_state(ctx, SeqState.ABORTED)
    assert ctx.iface.dio_word16() == ctx.bundle.channels.model.safe_word()


# ==================================================== H12: control-mode gating


async def test_fire_refused_without_control_mode(node):
    """FIRE, like ARM, requires control mode — disabling it must lock out firing."""
    import asyncio

    from tbctl.services.sequencer import SequencerError

    ctx = node
    _inject_quick(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    await ctx.sequencer.arm("quick", control_enabled=True)
    with pytest.raises(SequencerError, match="control mode"):
        await ctx.sequencer.fire("quick", control_enabled=False)


async def test_disable_control_mode_while_armed_aborts(client_ctx):
    """Disabling control mode while ARMED fails safe rather than leaving it live."""
    import asyncio

    ctx, client = client_ctx
    _inject_quick(ctx)
    await client.post("/api/control-mode", json={"enabled": True})
    await client.post("/api/sim/set", json={"tag": "pressure.n2_purge", "value": 9.0})
    await asyncio.sleep(0.3)
    await client.post("/api/sequences/quick/load")
    await client.post("/api/sequences/arm", json={"confirm": "quick"})
    assert ctx.sequencer.state() == SeqState.ARMED

    await client.post("/api/control-mode", json={"enabled": False})
    await _await_state(ctx, SeqState.ABORTED)


# ============================================================== M6: reset ARMED


async def test_reset_from_armed_is_clean_standdown(node):
    import asyncio

    ctx = node
    _inject_quick(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.3)
    await ctx.sequencer.load("quick")
    await ctx.sequencer.arm("quick", control_enabled=True)
    assert ctx.sequencer.state() == SeqState.ARMED
    # clean stand-down: IDLE, not an HB-timeout abort
    await ctx.sequencer.reset()
    assert ctx.sequencer.state() == SeqState.IDLE
    assert ctx.sequencer.error() == SeqError.NONE


# =================================================== C6/M5: precondition safety


async def test_precondition_rejects_nan(node):
    import asyncio

    ctx = node
    _inject_quick(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.2)
    await ctx.sequencer.load("quick")
    # freshness ok, but the guarded value is NaN -> must fail, never pass
    ctx.sequencer._acq.last_sample_age = lambda: 0.0
    ctx.sequencer._acq.latest_values = lambda: {"pressure.n2_purge": float("nan")}
    failures = ctx.sequencer.check_preconditions()
    assert any("non-finite" in f for f in failures)


async def test_precondition_rejects_stale_telemetry(node):
    import asyncio

    ctx = node
    _inject_quick(ctx)
    ctx.iface.sim_set("pressure.n2_purge", 9.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.2)
    await ctx.sequencer.load("quick")
    ctx.sequencer._acq.last_sample_age = lambda: 30.0  # stream wedged 30 s ago
    ctx.sequencer._acq.latest_values = lambda: {"pressure.n2_purge": 9.0}
    failures = ctx.sequencer.check_preconditions()
    assert any("stale" in f for f in failures)


# ================================================== C6: redline NaN handling


def test_redline_trips_on_persistent_nan():
    """A redlined sensor going non-finite must trip (fail-safe), and NaN must
    not reset the persistence streak."""
    import asyncio

    from tbctl.config.models import Redline
    from tbctl.services.redlines import RedlineWatcher

    class _Acq:
        def __init__(self):
            self.rings = {}

        def add_listener(self, fn):
            return lambda: None

    tripped = []

    async def _abort(reason):
        tripped.append(reason)

    class _Events:
        def log(self, *a, **k):
            pass

    w = RedlineWatcher(_Acq(), _Events(), _abort)
    rl = Redline.model_validate({"tag": "pressure.chamber", "max": 20.0, "persist_ms": 50})
    w._redlines = [rl]
    w._tag_cols = {"pressure.chamber": ("default", 0)}

    async def run():
        # 60 ms of NaN at 1 kHz -> exceeds the 50 ms persist window
        t = np.arange(60) / 1000.0
        block = np.full((60, 1), np.nan)
        w._on_block("default", t, block)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert tripped, "redline did not trip on persistent NaN"


# ================================================= C2: out-of-band safe escalation


async def test_force_safe_outputs_drives_all_outputs_safe(node):
    ctx = node
    chmap = ctx.bundle.channels.model
    # energize every output away from safe, then force safe out-of-band
    for c in chmap.outputs():
        ctx.iface.write_name(c.hw, float(chmap.active_level(c)))
    ctx.sequencer._force_safe_outputs()
    assert ctx.iface.dio_word16() == chmap.safe_word()


# ===================================================== H14: LJM dummy masking


def test_ljm_dummy_samples_masked(chmap_fixture):
    """-9999.0 stream-recovery placeholders become NaN, never a huge bogus EU."""
    from tbctl.hw.ljm_mock import MockDevice
    from tbctl.services.acquisition import AcquisitionService

    chmap = chmap_fixture
    acq = AcquisitionService(chmap, MockDevice(chmap))
    n_ch = len(acq._scan_names)
    block = np.zeros((4, n_ch))
    block[1, :] = -9999.0  # one dummy scan
    acq._ingest(block)
    vals = acq.latest_values()
    # nothing calibrated the dummy into a huge number
    assert all(abs(v) < 1e6 for v in vals.values() if v == v)  # v==v skips NaN
    assert acq.dropped_scans >= 1


@pytest.fixture
def chmap_fixture():
    from tbctl.config.loader import load_channels

    return load_channels(CONFIGS / "channels.yaml").model


# ================================================= H2: recorder always stoppable


async def test_recorder_stoppable_even_if_flush_fails(node):
    """A flush error must never leave the run un-stoppable (self._run stuck)."""
    import asyncio

    ctx = node
    await ctx.recorder.start_run("t", "op")
    await asyncio.sleep(0.2)

    # make every flush raise, as a disk-full / IO error would
    def boom():
        raise OSError("disk full")

    ctx.recorder._flush = boom
    run = await ctx.recorder.stop_run()  # must not raise
    assert ctx.recorder.active is None
    assert run.t_stop is not None
    # a fresh run can be started afterwards
    await ctx.recorder.start_run("t2", "op")
    assert ctx.recorder.active is not None
    await ctx.recorder.stop_run()
