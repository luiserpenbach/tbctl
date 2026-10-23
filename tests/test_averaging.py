"""Per-channel `average: true`: stream rate, validation, window means."""

import asyncio

import numpy as np
import pytest
from pydantic import ValidationError

from tbctl.config.models import AVERAGE_STREAM_HZ, ChannelMap
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services.acquisition import AcquisitionService

LIN = {"kind": "linear", "gain": 1.0, "offset": 0.0}


def _chmap(*channels, groups=None) -> ChannelMap:
    return ChannelMap.model_validate(
        {
            "device": {"ip": "192.168.10.20", "watchdog_timeout_s": 5, "record_outputs": False},
            "rate_groups": groups or {"default": {"rate_hz": 50}},
            "channels": list(channels),
        }
    )


def _ain(tag, hw, **kw):
    return {"tag": tag, "hw": hw, "kind": "analog_in", "unit": "V", "cal": LIN, **kw}


def test_stream_rate_raised_only_when_averaging():
    assert _chmap(_ain("p.a", "AIN0")).stream_rate_hz() == 50
    assert _chmap(_ain("p.a", "AIN0", average=True)).stream_rate_hz() == AVERAGE_STREAM_HZ
    fast = {"default": {"rate_hz": 50}, "fast": {"rate_hz": 2000}}
    m = _chmap(
        _ain("p.a", "AIN0", average=True), _ain("p.b", "AIN1", rate_group="fast"), groups=fast
    )
    assert m.stream_rate_hz() == 2000  # already faster: kept


def test_rate_groups_must_divide_the_averaged_stream():
    groups = {"default": {"rate_hz": 50}, "slow": {"rate_hz": 60}}
    with pytest.raises(ValidationError, match="must divide the stream rate"):
        _chmap(
            _ain("p.a", "AIN0", average=True), _ain("p.b", "AIN1", rate_group="slow"), groups=groups
        )
    # without averaging a 60 Hz group alone is fine (stream runs at 60 Hz)
    _chmap(
        _ain("p.b", "AIN1", rate_group="slow"),
        groups={"default": {"rate_hz": 60}, "slow": {"rate_hz": 60}},
    )


def test_average_at_stream_rate_is_rejected():
    with pytest.raises(ValidationError, match="no effect"):
        _chmap(_ain("p.a", "AIN0", average=True), groups={"default": {"rate_hz": 1000}})


def _rig(*channels):
    chmap = _chmap(*channels)
    acq = AcquisitionService(chmap, MockDevice(chmap))
    acq._stream_epoch = 1000.0
    return acq


def _feed(acq, volts, block=25):
    """Ingest in read-sized blocks (25 scans = 40 reads/s at 1 kHz)."""
    for i in range(0, len(volts), block):
        acq._ingest(volts[i : i + block])


def test_window_means_span_read_blocks():
    acq = _rig(_ain("p.avg", "AIN0", average=True), _ain("p.raw", "AIN1"))
    assert acq.scan_rate_hz == 1000 and acq._group_decim["default"] == 20
    n = 200  # 10 windows of 20 scans, fed in blocks of 25 (windows straddle reads)
    ramp = np.arange(n, dtype=float)
    _feed(acq, np.column_stack([ramp, ramp]))
    t, v = acq.rings["default"].window(-1.0)
    assert len(t) == 10
    ends = np.arange(19, n, 20)
    np.testing.assert_allclose(t, 1000.0 + ends / 1000.0)  # stamped at the window's last scan
    np.testing.assert_allclose(v[:, 0], ends - 9.5)  # mean of scans end-19..end
    np.testing.assert_allclose(v[:, 1], ends)  # non-averaged: the scan at that instant


def test_skipped_scans_are_left_out_of_the_mean():
    acq = _rig(_ain("p.avg", "AIN0", average=True))
    volts = np.full((20, 1), 2.0)
    volts[3:8, 0] = -9999.0  # LJM_DUMMY_VALUE
    _feed(acq, volts)
    _, v = acq.rings["default"].window(-1.0)
    assert v[0, 0] == pytest.approx(2.0)
    acq._ingest(np.full((20, 1), -9999.0))  # a window with no valid scan
    _, v = acq.rings["default"].window(-1.0)
    assert np.isnan(v[1, 0])


def test_restart_does_not_average_across_the_outage():
    acq = _rig(_ain("p.avg", "AIN0", average=True))
    acq._ingest(np.full((10, 1), 100.0))  # half a window before the cable is pulled
    acq._clear_avg_carry()  # what _restart_stream does
    acq._ingest(np.full((10, 1), 4.0))  # rest of the window after the restart
    _, v = acq.rings["default"].window(-1.0)
    assert v[0, 0] == pytest.approx(4.0)


async def test_averaging_cuts_noise_in_the_simulator():
    chmap = _chmap(_ain("p.avg", "AIN0", average=True), _ain("p.raw", "AIN1"))
    dev = MockDevice(chmap)
    dev.open()
    for tag in ("p.avg", "p.raw"):
        dev.sim_set(tag, 0.0, noise_sigma_eu=1.0)
    acq = AcquisitionService(chmap, dev)
    await acq.start()
    try:
        await asyncio.sleep(1.5)
        _, v = acq.rings["default"].window(-1.0)
    finally:
        await acq.stop()
    assert len(v) >= 40
    # 20-scan mean of white noise: sigma / sqrt(20) ~ 0.22
    assert np.std(v[:, 0]) < 0.4 < 0.7 < np.std(v[:, 1])


def test_rate_candidates_for_fallback():
    m = _chmap(_ain("p.a", "AIN0", average=True))
    assert m.stream_rate_candidates() == [1000, 500, 250, 200, 100]
    assert _chmap(_ain("p.a", "AIN0")).stream_rate_candidates() == [50]  # no averaging: no fallback
    groups = {"default": {"rate_hz": 50}, "fast": {"rate_hz": 200}}
    m = _chmap(
        _ain("p.a", "AIN0", average=True), _ain("p.b", "AIN1", rate_group="fast"), groups=groups
    )
    assert m.stream_rate_candidates() == [1000, 200]  # 200 Hz must divide the stream rate


async def test_falls_back_to_the_fastest_rate_the_device_accepts(caplog):
    chmap = _chmap(_ain("p.avg", "AIN0", average=True), _ain("p.raw", "AIN1"))
    dev = MockDevice(chmap)
    dev.max_stream_hz = 150  # like the bench T7 with three +-0.1 V inputs (only 100 Hz works)
    dev.open()
    acq = AcquisitionService(chmap, dev)
    with caplog.at_level("INFO"):
        await acq.start()
    try:
        assert acq.scan_rate_hz == 100 and acq._group_decim["default"] == 2
        assert "cannot scan the list at 1000 Hz" in caplog.text
        assert "stream at 100 Hz; averaged channels use default: 2 scans per sample" in caplog.text
        await asyncio.sleep(0.5)
        t, _ = acq.rings["default"].window(-1.0)
        assert len(t) >= 15 and np.allclose(np.diff(t), 0.02, atol=1e-5)  # still 50 Hz samples
    finally:
        await acq.stop()


async def test_restart_resumes_at_the_rate_that_worked(monkeypatch):
    from tbctl.services import acquisition

    monkeypatch.setattr(acquisition, "RESTART_MAX_DELAY_S", 0.05)
    chmap = _chmap(_ain("p.avg", "AIN0", average=True))

    class Pullable(MockDevice):
        max_stream_hz = 300
        unplugged = False

        def __init__(self, chmap):
            super().__init__(chmap)
            self.started: list[float] = []

        def stream_start(self, names, rate, spr):
            self.started.append(rate)
            self.unplugged = False
            return super().stream_start(names, rate, spr)

        def stream_read(self):
            if self.unplugged:
                raise RuntimeError("LJME_NO_RESPONSE_BYTES_RECEIVED")
            return super().stream_read()

    dev = Pullable(chmap)
    dev.open()
    acq = AcquisitionService(chmap, dev)
    await acq.start()
    try:
        assert dev.started == [1000, 500, 250]
        dev.unplugged = True
        for _ in range(100):  # <= 2 s for the restart
            if len(dev.started) > 3 and acq.stream_ok:
                break
            await asyncio.sleep(0.02)
        assert dev.started == [1000, 500, 250, 250]  # no second probe from 1 kHz
        assert acq.scan_rate_hz == 250
    finally:
        await acq.stop()
