"""Ring buffer, EU conversion, decimation, and mock streaming."""

import numpy as np
import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services.acquisition import AcquisitionService, RingBuffer, envelope_decimate
from tbctl.services.thermocouple import (
    compensated_temp_degc,
    type_k_emf_mv,
    type_k_temp_degc,
)
from tests.conftest import CONFIGS


@pytest.fixture
def chmap():
    return load_channels(CONFIGS / "channels.yaml").model


# --------------------------------------------------------------- ring buffer


def test_ring_append_and_window():
    rb = RingBuffer(["a.x", "b.y"], rate_hz=50, capacity_s=2)
    t = np.arange(10) / 50.0
    v = np.stack([t * 1.0, t * 2.0], axis=1)
    rb.append(t, v)
    tw, vw = rb.window(0.05)
    assert len(tw) == 7  # strictly after t0
    assert vw.shape == (7, 2)
    assert rb.latest() == {"a.x": pytest.approx(t[-1]), "b.y": pytest.approx(2 * t[-1])}


def test_ring_wraps():
    rb = RingBuffer(["a.x"], rate_hz=10, capacity_s=1)  # capacity 10
    for k in range(5):
        t = (k * 10 + np.arange(10)) / 10.0
        rb.append(t, t.reshape(-1, 1))
    tw, _vw = rb.window(0.0)
    assert len(tw) == 10
    assert tw[0] == pytest.approx(4.0)
    assert tw[-1] == pytest.approx(4.9)


def test_envelope_decimate():
    t = np.arange(1000) / 100.0
    v = np.sin(t)
    out = envelope_decimate(t, v, 10)
    assert len(out["t"]) == 10
    assert all(mn <= mx for mn, mx in zip(out["min"], out["max"], strict=True))
    # passthrough when small enough
    small = envelope_decimate(t[:5], v[:5], 10)
    assert small["mean"] == small["min"] == small["max"]


# ------------------------------------------------------------- thermocouple


def test_type_k_anchors():
    # NIST table anchors (mV at 0 / 100 / 500 / 1000 degC)
    assert type_k_emf_mv(0.0) == pytest.approx(0.0, abs=1e-6)
    assert type_k_emf_mv(100.0) == pytest.approx(4.096, abs=0.005)
    assert type_k_emf_mv(500.0) == pytest.approx(20.644, abs=0.01)
    assert type_k_emf_mv(1000.0) == pytest.approx(41.276, abs=0.02)


def test_type_k_roundtrip():
    # forward and inverse are independent NIST fits — agreement validates both
    for t in [-150, -50, 0.01, 25, 100, 300, 499, 600, 900, 1200]:
        assert type_k_temp_degc(type_k_emf_mv(t)) == pytest.approx(t, abs=0.1)


def test_cjc():
    # 300 degC hot junction measured with 25 degC cold junction
    v_meas = (type_k_emf_mv(300.0) - type_k_emf_mv(25.0)) / 1000.0
    assert compensated_temp_degc(v_meas, 25.0) == pytest.approx(300.0, abs=0.1)


# ------------------------------------------------------- mock + acquisition


def test_mock_stream_produces_setpoint_volts(chmap):
    mock = MockDevice(chmap)
    mock.open()
    mock.sim_set("pressure.chamber", 10.0, noise_sigma_eu=0.0)  # 25 bar/V -> 0.4 V
    rate = mock.stream_start(["AIN0"], 50.0, 5)
    assert rate == 50.0
    block, _ = mock.stream_read()
    assert block.shape == (5, 1)
    assert block.mean() == pytest.approx(0.4, abs=0.02)  # wobble ±1%


async def test_acquisition_end_to_end(chmap):
    mock = MockDevice(chmap)
    mock.open()
    mock.sim_set("pressure.chamber", 10.0, noise_sigma_eu=0.0)
    mock.sim_set("temp.injector", 150.0, noise_sigma_eu=0.0)
    acq = AcquisitionService(chmap, mock)
    await acq.start()
    try:
        import asyncio

        await asyncio.sleep(0.5)
        vals = acq.latest_values()
        assert vals["pressure.chamber"] == pytest.approx(10.0, rel=0.05)
        assert vals["temp.injector"] == pytest.approx(150.0, rel=0.05)
        win = acq.live_window(["pressure.chamber"], window_s=10, max_points=100)
        assert len(win["pressure.chamber"]["t"]) > 5
        frames = acq.frames_since(0.0, ["pressure.chamber"])
        assert frames and frames[0]["tags"] == ["pressure.chamber"]
        assert acq.sample_count > 0
    finally:
        await acq.stop()


def test_mock_dio_and_manual_valve(chmap):
    mock = MockDevice(chmap)
    mock.open()
    # power-on image = safe word
    assert mock.dio_word16() == chmap.safe_word()
    mock.write_name("FIO0", 1)
    assert mock.read_name("FIO0") == 1.0
    assert mock.dio_word16() & 1


def test_ring_reads_match_brute_force_across_wraps():
    """window()/latest() index the ring in place; check them against the
    time-ordered contents for many fill levels, including wrapped ones."""
    rng = np.random.default_rng(7)
    ring = RingBuffer(["a", "b"], rate_hz=10, capacity_s=5)  # 50 rows
    all_t, all_v = np.empty(0), np.empty((0, 2))
    t_next = 0.0
    for _ in range(40):
        n = int(rng.integers(1, 23))
        t = t_next + np.arange(n) * 0.1
        t_next = t[-1] + 0.1
        v = rng.random((n, 2))
        ring.append(t, v)
        all_t, all_v = np.concatenate([all_t, t])[-50:], np.concatenate([all_v, v])[-50:]
        assert ring.latest() == {"a": all_v[-1, 0], "b": all_v[-1, 1]}
        for t0, t1 in [(-1.0, None), (all_t[0], None), (all_t[-5], all_t[-2]), (all_t[-1], None)]:
            got_t, got_v = ring.window(t0, t1)
            keep = (all_t > t0) & ((all_t <= t1) if t1 is not None else True)
            np.testing.assert_array_equal(got_t, all_t[keep])
            np.testing.assert_array_equal(got_v, all_v[keep])
