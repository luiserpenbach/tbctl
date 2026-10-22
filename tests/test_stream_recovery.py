"""Stream recovery after a device outage (USB pull on a bus-powered T7),
non-blocking stream reads, and the streamed cold junction."""

import asyncio
import math
import time

import numpy as np
import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services import acquisition
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.thermocouple import (
    compensated_temp_degc,
    t7_temp_sensor_degc,
    t7_temp_sensor_volts,
    type_k_temp_degc,
)
from tests.conftest import CONFIGS


class FlakyDevice(MockDevice):
    """MockDevice whose stream fails for a while, like a pulled cable: the
    read errors once, then every restart fails until `outage_s` has passed.
    A restart also resets the AIN settings, as a power-cycled T7 does."""

    def __init__(self, chmap, outage_s: float):
        super().__init__(chmap)
        self._ain_settings = chmap.ain_settings()
        self.outage_s = outage_s
        self.fail_at: float | None = None
        self.unplugged = False
        self.starts = 0

    def stream_start(self, scan_names, scan_rate_hz, scans_per_read):
        if self.fail_at is not None and time.monotonic() - self.fail_at < self.outage_s:
            raise RuntimeError("LJME_RECONNECT_FAILED")
        self.unplugged = False
        self.starts += 1
        return super().stream_start(scan_names, scan_rate_hz, scans_per_read)

    def stream_read(self):
        if self.unplugged:
            raise RuntimeError("LJME_NO_RESPONSE_BYTES_RECEIVED")
        return super().stream_read()

    def pull_cable(self) -> None:
        self.fail_at = time.monotonic()
        for name, _ in self._ain_settings:
            self.write_name(name, 10.0 if name.endswith("_RANGE") else 199)
        self.unplugged = True


@pytest.fixture
def chmap():
    return load_channels(CONFIGS / "channels.yaml").model


async def _wait(cond, timeout=5.0):
    t_end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < t_end, "timed out"
        await asyncio.sleep(0.02)


async def test_restart_leaves_gap_and_reapplies_ain(chmap, monkeypatch):
    monkeypatch.setattr(acquisition, "STREAM_RESTART_S", 0.05)
    dev = FlakyDevice(chmap, outage_s=0.5)
    dev.open()
    acq = AcquisitionService(chmap, dev)
    gaps: list[tuple[float, float]] = []

    async def hook(gap_start, gap_end):
        gaps.append((gap_start, gap_end))

    acq.add_restart_hook(hook)
    await acq.start()
    try:
        await _wait(lambda: acq.sample_count > 10)
        dev.pull_cable()
        await _wait(lambda: not acq.stream_ok)
        count_during = acq.sample_count
        await _wait(lambda: gaps and acq.sample_count > count_during + 10)
        assert acq.stream_ok and acq.stream_error is None
        assert dev.starts == 2
        # AIN range / negative_ch written again before the restarted stream
        for name, value in chmap.ain_settings():
            assert dev.read_name(name) == value
        gap_start, gap_end = gaps[0]
        assert gap_end - gap_start == pytest.approx(0.5, abs=0.25)
        # the run's time axis shows the outage, and resumes at wall-clock time
        t, _ = acq.rings["default"].window(-1.0)
        dt = np.diff(t)
        assert dt.max() == pytest.approx(gap_end - gap_start + 1 / acq.scan_rate_hz, abs=0.05)
        assert np.sum(dt > 0.1) == 1
        assert time.time() - t[-1] < 0.5
    finally:
        await acq.stop()


async def test_empty_reads_are_polled(chmap):
    """A non-blocking eStreamRead returns no scans; the loop polls again."""

    class EmptyFirst(MockDevice):
        empties = 0

        def stream_read(self):
            if self.empties < 5:
                self.empties += 1
                return np.empty((0, len(self._scan_names))), 0
            return super().stream_read()

    dev = EmptyFirst(chmap)
    dev.open()
    acq = AcquisitionService(chmap, dev)
    await acq.start()
    try:
        await _wait(lambda: acq.sample_count > 5)
        assert dev.empties == 5 and acq.stream_ok
    finally:
        await acq.stop()


async def test_cold_junction_from_streamed_sensor(chmap):
    dev = MockDevice(chmap)
    dev.open()
    dev._regs["TEMPERATURE_DEVICE_K"] = 273.15 + 37.0
    acq = AcquisitionService(chmap, dev)
    assert acq._scan_names[-1] == "AIN14"
    await acq.start()
    try:
        await _wait(lambda: acq.sample_count > 5)
        assert acq.device_temp_degc == pytest.approx(37.0, abs=0.01)
    finally:
        await acq.stop()


def test_t7_temp_sensor_roundtrip():
    # datasheet: TEMPERATURE_DEVICE_K = -92.6 * AIN14 + 467.6
    assert t7_temp_sensor_degc(1.8) == pytest.approx(-92.6 * 1.8 + 467.6 - 273.15)
    assert t7_temp_sensor_degc(t7_temp_sensor_volts(27.4)) == pytest.approx(27.4)


@pytest.mark.parametrize("v", [-0.1058, 0.10105, -1.37])
def test_saturated_thermocouple_is_nan(v):
    # bench readings from an open / unwired input on the +-0.1 V (and +-10 V) range
    assert math.isnan(compensated_temp_degc(v, 25.0))


def test_type_k_range_edges():
    assert type_k_temp_degc(-5.891) == pytest.approx(-200.0, abs=0.5)
    assert type_k_temp_degc(54.886) == pytest.approx(1372.0, abs=0.5)
    assert math.isnan(type_k_temp_degc(-5.95)) and math.isnan(type_k_temp_degc(55.0))
    assert math.isnan(compensated_temp_degc(0.001, math.nan))


@pytest.fixture
async def node(tmp_path):
    import httpx

    from tbctl.node import build_app, build_context

    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.ctx = ctx
            yield c


async def test_status_reports_unreachable_device(node, monkeypatch):
    def unreachable(*_a, **_k):
        raise RuntimeError("LJME_RECONNECT_FAILED")

    monkeypatch.setattr(node.ctx.iface, "read_names", unreachable)
    r = await node.get("/api/status")
    assert r.status_code == 200
    s = r.json()
    assert s["device"]["connected"] is False
    assert "RECONNECT_FAILED" in s["device"]["error"]
    assert s["sequencer"]["state"] == "UNKNOWN" and s["outputs"] == {}


async def test_restart_hook_logs_event_and_checks_lua(node):
    from tbctl.hw.registers_gen import REG_NAME

    ctx = node.ctx
    (hook,) = ctx.acq._restart_hooks
    await hook(100.0, 109.35)
    events = [e["event"] for e in ctx.events.tail(10)]
    assert "stream_restart" in events and "lua_not_running" not in events
    restart = next(e for e in ctx.events.tail(10) if e["event"] == "stream_restart")
    assert restart["outage_s"] == pytest.approx(9.35)
    # power-cycled T7 without a flashed script: interp_version reads 0
    ctx.iface.backend._regs[REG_NAME["INTERP_VERSION"]] = 0.0
    await hook(200.0, 201.0)
    assert ctx.events.tail(1)[0]["event"] == "lua_not_running"
