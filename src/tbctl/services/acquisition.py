"""Acquisition: stream config, EU conversion, ring buffers (spec §3).

One T7 stream runs at the fastest rate any configured channel needs; slower
rate groups are decimated by an integer factor into their own ring buffers.
v1 ships with everything at 50 Hz, but the rate-group mechanism already
carries a future 1 kHz group (D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

import numpy as np

from tbctl.config.models import (
    AnalogInChannel,
    ChannelMap,
    InputChannel,
    LinearCal,
    PolynomialCal,
    TableCal,
    ThermocoupleChannel,
)
from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.services.thermocouple import compensated_temp_degc

log = logging.getLogger(__name__)

CJC_POLL_S = 1.0  # cold-junction temperature refresh interval


class RingBuffer:
    """Fixed-capacity circular buffer for one rate group (shared time base)."""

    def __init__(self, tags: list[str], rate_hz: float, capacity_s: float = 600.0):
        self.tags = list(tags)
        self.rate_hz = rate_hz
        self.capacity = max(1, int(capacity_s * rate_hz))
        self._t = np.zeros(self.capacity, dtype=np.float64)
        self._v = np.full((self.capacity, len(tags)), np.nan, dtype=np.float64)
        self._write = 0  # next write slot
        self._count = 0
        self._col = {tag: i for i, tag in enumerate(self.tags)}

    def append(self, t: np.ndarray, block: np.ndarray) -> None:
        n = len(t)
        if n == 0:
            return
        if n >= self.capacity:  # keep only the newest capacity samples
            t, block = t[-self.capacity :], block[-self.capacity :]
            n = self.capacity
        idx = (self._write + np.arange(n)) % self.capacity
        self._t[idx] = t
        self._v[idx] = block
        self._write = int((self._write + n) % self.capacity)
        self._count = min(self._count + n, self.capacity)

    def _ordered(self) -> tuple[np.ndarray, np.ndarray]:
        if self._count < self.capacity:
            return self._t[: self._count], self._v[: self._count]
        idx = (self._write + np.arange(self.capacity)) % self.capacity
        return self._t[idx], self._v[idx]

    def window(self, t0: float, t1: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Samples with t0 < t <= t1 (copies), oldest first."""
        t, v = self._ordered()
        if len(t) == 0:
            return t, v
        lo = np.searchsorted(t, t0, side="right")
        hi = len(t) if t1 is None else int(np.searchsorted(t, t1, side="right"))
        return t[lo:hi].copy(), v[lo:hi].copy()

    def latest(self) -> dict[str, float]:
        t, v = self._ordered()
        if len(t) == 0:
            return {}
        return {tag: float(v[-1, i]) for tag, i in self._col.items()}

    def columns(self, tags: list[str]) -> list[int]:
        return [self._col[t] for t in tags]


def envelope_decimate(t: np.ndarray, v: np.ndarray, max_points: int) -> dict[str, list[float]]:
    """Min/max envelope decimation of one series to <= max_points buckets."""
    n = len(t)
    if n == 0:
        return {"t": [], "mean": [], "min": [], "max": []}
    if n <= max_points:
        vals = v.tolist()
        return {"t": t.tolist(), "mean": vals, "min": vals, "max": vals}
    buckets = np.array_split(np.arange(n), max_points)
    out_t, out_mean, out_min, out_max = [], [], [], []
    for b in buckets:
        seg = v[b]
        out_t.append(float(t[b[len(b) // 2]]))
        out_mean.append(float(np.nanmean(seg)))
        out_min.append(float(np.nanmin(seg)))
        out_max.append(float(np.nanmax(seg)))
    return {"t": out_t, "mean": out_mean, "min": out_min, "max": out_max}


def _make_converter(ch: InputChannel) -> Callable[[np.ndarray, float], np.ndarray]:
    """volts (array), cold-junction degC -> engineering units."""
    if isinstance(ch, ThermocoupleChannel):

        def tc(v: np.ndarray, cj_degc: float) -> np.ndarray:
            out = np.array([compensated_temp_degc(x, cj_degc) for x in v])
            if ch.unit == "K":
                out += 273.15
            elif ch.unit == "degF":
                out = out * 9.0 / 5.0 + 32.0
            return out

        return tc
    assert isinstance(ch, AnalogInChannel)
    cal = ch.cal
    if isinstance(cal, LinearCal):
        return lambda v, _cj: v * cal.gain + cal.offset
    if isinstance(cal, PolynomialCal):
        coeffs = list(reversed(cal.coeffs))  # np.polyval wants highest first
        return lambda v, _cj: np.polyval(coeffs, v)
    if isinstance(cal, TableCal):
        return lambda v, _cj: np.interp(v, cal.x, cal.y)
    raise TypeError(f"unknown cal {cal!r}")


class AcquisitionService:
    """Owns the device stream and one ring buffer per rate group."""

    def __init__(
        self,
        chmap: ChannelMap,
        iface: DeviceInterface,
        capacity_s: float = 600.0,
    ):
        self._iface = iface
        self._task: asyncio.Task | None = None
        self._stream_epoch = 0.0
        self._samples_in = 0
        self.dropped_scans = 0
        self._capacity_s = capacity_s
        # listeners get (group_name, t_epoch, eu_block) after every ingest;
        # survive reconfigure() so the recorder hook stays attached
        self._listeners: list[Callable[[str, np.ndarray, np.ndarray], None]] = []
        self._configure(chmap, capacity_s)

    def _configure(self, chmap: ChannelMap, capacity_s: float) -> None:
        self._chmap = chmap
        inputs = chmap.inputs()
        self._scan_channels = inputs  # scan list order == channel order
        self._scan_names = [c.hw for c in inputs]
        self._converters = [_make_converter(c) for c in inputs]
        self.scan_rate_hz = max(
            (chmap.rate_groups[c.rate_group].rate_hz for c in inputs), default=50.0
        )
        # per rate group: decimation factor + ring buffer
        self.rings: dict[str, RingBuffer] = {}
        self._group_cols: dict[str, list[int]] = {}
        self._group_decim: dict[str, int] = {}
        for group, rg in chmap.rate_groups.items():
            tags = [c.tag for c in inputs if c.rate_group == group]
            if not tags:
                continue
            factor = self.scan_rate_hz / rg.rate_hz
            if abs(factor - round(factor)) > 1e-9:
                raise ValueError(
                    f"rate group {group}: {rg.rate_hz} Hz must divide stream rate "
                    f"{self.scan_rate_hz} Hz"
                )
            self._group_decim[group] = round(factor)
            self._group_cols[group] = [self._scan_names.index(chmap.by_tag(t).hw) for t in tags]
            self.rings[group] = RingBuffer(tags, rg.rate_hz, capacity_s)
        self._tag_group = {c.tag: c.rate_group for c in inputs}
        self._cj_degc = 25.0
        self._cj_read_at = 0.0
        # latest raw volts per tag (for the raw channel viewer)
        self._latest_raw: dict[str, float] = {}

    def reconfigure(self, chmap: ChannelMap) -> None:
        """Apply a new channel map in place (must be stopped). Ring buffers
        reset; the service object identity is preserved so every holder's
        reference stays valid."""
        if self.running:
            raise RuntimeError("stop acquisition before reconfigure")
        self._configure(chmap, self._capacity_s)

    def add_listener(self, fn: Callable[[str, np.ndarray, np.ndarray], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        scans_per_read = max(1, int(self.scan_rate_hz / 10))  # ~10 Hz loop
        actual = await asyncio.to_thread(
            self._iface.stream_start, self._scan_names, self.scan_rate_hz, scans_per_read
        )
        if abs(actual - self.scan_rate_hz) / self.scan_rate_hz > 0.01:
            log.warning("device stream rate %.1f != requested %.1f", actual, self.scan_rate_hz)
        self.scan_rate_hz = actual
        self._stream_epoch = time.time()
        self._samples_in = 0
        self._task = asyncio.create_task(self._run(), name="acquisition")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        await asyncio.to_thread(self._iface.stream_stop)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while True:
            try:
                block, backlog = await asyncio.to_thread(self._iface.stream_read)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("stream read failed; retrying in 1 s")
                await asyncio.sleep(1.0)
                continue
            if backlog > self.scan_rate_hz:  # >1 s behind
                log.warning("stream backlog %d scans", backlog)
            self._ingest(block)

    # -- data path ---------------------------------------------------------

    def _ingest(self, volts: np.ndarray) -> None:
        n = len(volts)
        if n == 0:
            return
        now = time.time()
        if now - self._cj_read_at > CJC_POLL_S:
            self._cj_read_at = now
            with contextlib.suppress(Exception):
                self._cj_degc = self._iface.read_name("TEMPERATURE_DEVICE_K") - 273.15
        idx = self._samples_in + np.arange(n)
        t = self._stream_epoch + idx / self.scan_rate_hz
        eu = np.empty_like(volts)
        for j, conv in enumerate(self._converters):
            eu[:, j] = conv(volts[:, j], self._cj_degc)
            self._latest_raw[self._scan_channels[j].tag] = float(volts[-1, j])
        for group, ring in self.rings.items():
            decim = self._group_decim[group]
            mask = idx % decim == 0
            if mask.any():
                gt = t[mask]
                gv = eu[np.ix_(mask, self._group_cols[group])]
                ring.append(gt, gv)
                for fn in self._listeners:
                    try:
                        fn(group, gt, gv)
                    except Exception:
                        log.exception("acquisition listener failed")
        self._samples_in += n

    # -- queries -----------------------------------------------------------

    @property
    def sample_count(self) -> int:
        return self._samples_in

    def live_window(
        self, tags: list[str], window_s: float, max_points: int = 2000
    ) -> dict[str, dict[str, list[float]]]:
        """Per-tag decimated envelope for chart backfill (§7 GET /api/live)."""
        now = time.time()
        out: dict[str, dict[str, list[float]]] = {}
        for tag in tags:
            group = self._tag_group.get(tag)
            if group is None:
                raise KeyError(f"unknown input tag {tag!r}")
            ring = self.rings[group]
            t, v = ring.window(now - window_s)
            col = ring.columns([tag])[0]
            out[tag] = envelope_decimate(t, v[:, col], max_points)
        return out

    def frames_since(self, t_last: float, tags: list[str] | None = None) -> list[dict]:
        """Raw new samples per rate group since t_last, for WS `frame` messages."""
        frames = []
        for ring in self.rings.values():
            sel = [tag for tag in ring.tags if tags is None or tag in tags]
            if not sel:
                continue
            t, v = ring.window(t_last)
            if len(t) == 0:
                continue
            cols = ring.columns(sel)
            frames.append(
                {
                    "type": "frame",
                    "t0": float(t[0]),
                    "dt": 1.0 / ring.rate_hz,
                    "t": t.tolist(),
                    "tags": sel,
                    "data": [v[:, c].tolist() for c in cols],
                }
            )
        return frames

    def latest_values(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for ring in self.rings.values():
            out.update(ring.latest())
        return out

    def latest_raw(self) -> dict[str, float]:
        """Most recent raw voltage per input tag (pre-calibration)."""
        return dict(self._latest_raw)
