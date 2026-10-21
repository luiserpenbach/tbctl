"""D4 validation: the fast (1 kHz) rate group works end-to-end — stream at the
max group rate, integer decimation into per-group rings, recorder groups, and
historical reads. This is the config the future 1 kHz chamber PT will use."""

import asyncio

import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.recorder import Recorder

MULTIRATE_YAML = """
device:
  ip: 192.168.10.20
  watchdog_timeout_s: 5
rate_groups:
  default: {rate_hz: 50}
  fast:    {rate_hz: 1000}
channels:
  - tag: pressure.chamber
    hw: AIN0
    kind: analog_in
    unit: bar
    cal: {kind: polynomial, coeffs: [0.0, 25.0]}
    rate_group: fast
  - tag: pressure.ox_manifold
    hw: AIN1
    kind: analog_in
    unit: bar
    cal: {kind: polynomial, coeffs: [0.0, 25.0]}
    rate_group: default
  - tag: temp.injector
    hw: AIN48
    kind: thermocouple_k
    unit: degC
    rate_group: default
  - tag: valve.ox_main
    hw: FIO0
    kind: valve
    fail_safe: closed
"""


@pytest.fixture
def chmap_loaded(tmp_path):
    p = tmp_path / "channels.yaml"
    p.write_text(MULTIRATE_YAML)
    return load_channels(p)


@pytest.fixture
async def rig(chmap_loaded, tmp_path):
    mock = MockDevice(chmap_loaded.model)
    mock.open()
    mock.sim_set("pressure.chamber", 12.0, noise_sigma_eu=0.0)
    mock.sim_set("pressure.ox_manifold", 20.0, noise_sigma_eu=0.0)
    acq = AcquisitionService(chmap_loaded.model, mock)
    rec = Recorder(chmap_loaded.model, chmap_loaded.sha256, acq, tmp_path / "runs", "test")
    await acq.start()
    yield mock, acq, rec
    await acq.stop()


async def test_stream_runs_at_max_group_rate(rig):
    _mock, acq, _rec = rig
    assert acq.scan_rate_hz == 1000.0
    assert set(acq.rings) == {"default", "fast"}
    assert acq.rings["fast"].rate_hz == 1000
    assert acq.rings["default"].rate_hz == 50
    # outputs ride along in the fastest group as 0/1 state channels
    assert acq.rings["fast"].tags == ["pressure.chamber", "valve.ox_main"]
    assert sorted(acq.rings["default"].tags) == ["pressure.ox_manifold", "temp.injector"]


async def test_decimation_ratios_and_values(rig):
    _mock, acq, _rec = rig
    await asyncio.sleep(1.0)
    fast_t, fast_v = acq.rings["fast"].window(0)
    slow_t, slow_v = acq.rings["default"].window(0)
    # ~1 s: fast ring ~1000 samples, default ~50 (20:1 decimation)
    assert len(fast_t) > 700
    assert 15 <= len(slow_t) <= len(fast_t) / 15
    ratio = len(fast_t) / len(slow_t)
    assert 15 < ratio < 25  # nominal 20
    # engineering units survive both paths
    assert fast_v[-1, 0] == pytest.approx(12.0, rel=0.05)
    col = acq.rings["default"].columns(["pressure.ox_manifold"])[0]
    assert slow_v[-1, col] == pytest.approx(20.0, rel=0.05)
    # both timebases strictly increasing at their own rates
    assert float(fast_t[1] - fast_t[0]) == pytest.approx(0.001, rel=0.2)
    assert float(slow_t[1] - slow_t[0]) == pytest.approx(0.02, rel=0.2)


async def test_recorder_captures_both_groups(rig):
    _mock, _acq, rec = rig
    run = await rec.start_run("multirate", "test")
    await asyncio.sleep(1.0)
    stopped = await rec.stop_run()
    assert stopped.sample_counts["fast"] > 700
    assert 15 <= stopped.sample_counts["default"] <= 80
    # historical decimated read per tag works for both rates
    data = rec.run_data(run.run_id, ["pressure.chamber", "pressure.ox_manifold"], max_points=200)
    assert 0 < len(data["pressure.chamber"]["t"]) <= 200
    assert data["pressure.chamber"]["mean"][-1] == pytest.approx(12.0, rel=0.05)
    assert data["pressure.ox_manifold"]["mean"][-1] == pytest.approx(20.0, rel=0.05)


async def test_live_window_and_frames_per_group(rig):
    _mock, acq, _rec = rig
    await asyncio.sleep(0.5)
    win = acq.live_window(["pressure.chamber", "temp.injector"], window_s=10, max_points=100)
    assert 0 < len(win["pressure.chamber"]["t"]) <= 100  # decimated from ~500 raw
    frames = acq.frames_since(0.0)
    dts = {f["dt"] for f in frames}
    assert 0.001 in dts and 0.02 in dts  # one frame per rate group


def test_non_integer_decimation_rejected(tmp_path):
    bad = MULTIRATE_YAML.replace("rate_hz: 50}", "rate_hz: 300}")  # 1000/300 not integer
    p = tmp_path / "channels.yaml"
    p.write_text(bad)
    loaded = load_channels(p)
    mock = MockDevice(loaded.model)
    with pytest.raises(ValueError, match="must divide"):
        AcquisitionService(loaded.model, mock)
