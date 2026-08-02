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
import threading
import time
from collections.abc import Callable

import numpy as np

from tbctl.config.expr import compile_expr
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
# LJM eStreamRead emits this placeholder for scans lost to stream
# auto-recovery. A real AIN reading never sits below the ±10 V range, so any
# sample this far negative is a dummy and must not be calibrated/recorded.
LJM_DUMMY_SENTINEL = -9999.0
_DUMMY_THRESHOLD = -9000.0


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
        # append() runs on the event loop; window()/latest()/_ordered() run on
        # FastAPI threadpool threads. Without this lock a reader can observe a
        # torn wraparound window with non-monotonic timestamps, which then
        # breaks the searchsorted slicing in window().
        self._lock = threading.Lock()

    def append(self, t: np.ndarray, block: np.ndarray) -> None:
        n = len(t)
        if n == 0:
            return
        if n >= self.capacity:  # keep only the newest capacity samples
            t, block = t[-self.capacity :], block[-self.capacity :]
            n = self.capacity
        idx = (self._write + np.arange(n)) % self.capacity
        with self._lock:
            self._t[idx] = t
            self._v[idx] = block
            self._write = int((self._write + n) % self.capacity)
            self._count = min(self._count + n, self.capacity)

    def _ordered(self) -> tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._count < self.capacity:
                return self._t[: self._count].copy(), self._v[: self._count].copy()
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

    def latest_time(self) -> float | None:
        with self._lock:
            if self._count == 0:
                return None
            last = (self._write - 1) % self.capacity
            return float(self._t[last])

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
        # wall-clock time of the last ingested block, for staleness checks that
        # must be robust to the synthetic sample-index timebase drifting
        self._last_ingest_wall: float | None = None
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
        # derived channels: compiled expression + which group they compute on.
        # A derived channel lives in the ring of its fastest referenced
        # channel; references to slower groups are sample-and-hold.
        self._derived_by_group: dict[str, list[tuple[str, object, list[str]]]] = {}
        derived_group_of: dict[str, str] = {}
        for dc in chmap.derived():
            fn, refs = compile_expr(dc.expr)
            g = chmap.derived_group(dc)
            self._derived_by_group.setdefault(g, []).append((dc.tag, fn, refs))
            derived_group_of[dc.tag] = g
        # per rate group: decimation factor + ring buffer
        self.rings: dict[str, RingBuffer] = {}
        self._group_cols: dict[str, list[int]] = {}
        self._group_decim: dict[str, int] = {}
        self._group_phys: dict[str, dict[str, int]] = {}  # tag -> col within group block
        for group, rg in chmap.rate_groups.items():
            tags = [c.tag for c in inputs if c.rate_group == group]
            dtags = [t for t, _, _ in self._derived_by_group.get(group, [])]
            if not tags and not dtags:
                continue
            if not tags:
                raise ValueError(
                    f"rate group {group}: derived channels need at least one "
                    "physical channel in their group"
                )
            factor = self.scan_rate_hz / rg.rate_hz
            if abs(factor - round(factor)) > 1e-9:
                raise ValueError(
                    f"rate group {group}: {rg.rate_hz} Hz must divide stream rate "
                    f"{self.scan_rate_hz} Hz"
                )
            self._group_decim[group] = round(factor)
            self._group_cols[group] = [self._scan_names.index(chmap.by_tag(t).hw) for t in tags]
            self._group_phys[group] = {t: k for k, t in enumerate(tags)}
            self.rings[group] = RingBuffer(tags + dtags, rg.rate_hz, capacity_s)
        self._tag_group = {c.tag: c.rate_group for c in inputs} | derived_group_of
        # latest EU value per physical tag, for cross-group sample-and-hold
        self._latest_eu: dict[str, float] = {}
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
            # Refresh the cold-junction temperature off the event loop — a real
            # LJM register read is a device round-trip and must never block the
            # loop (it would starve the heartbeat and the abort endpoint).
            now = time.time()
            if now - self._cj_read_at > CJC_POLL_S:
                self._cj_read_at = now
                try:
                    cj = await asyncio.to_thread(self._iface.read_name, "TEMPERATURE_DEVICE_K")
                    self._cj_degc = cj - 273.15
                except Exception:
                    log.debug("CJC read failed", exc_info=True)
            # An unexpected ingest error must not kill the acquisition task (it
            # would silently freeze telemetry and later re-raise out of stop()).
            try:
                self._ingest(block)
            except Exception:
                log.exception("ingest failed; dropping block")

    # -- data path ---------------------------------------------------------

    def _ingest(self, volts: np.ndarray) -> None:
        n = len(volts)
        if n == 0:
            return
        # Drop LJM stream-recovery placeholder samples before they can be
        # calibrated (x gain -> huge bogus EU), recorded, or redline-evaluated.
        dummy = volts < _DUMMY_THRESHOLD
        if dummy.any():
            self.dropped_scans += int(dummy.any(axis=1).sum())
            volts = np.where(dummy, np.nan, volts)
        self._last_ingest_wall = time.time()
        idx = self._samples_in + np.arange(n)
        t = self._stream_epoch + idx / self.scan_rate_hz
        eu = np.empty_like(volts)
        for j, conv in enumerate(self._converters):
            eu[:, j] = conv(volts[:, j], self._cj_degc)
            self._latest_raw[self._scan_channels[j].tag] = float(volts[-1, j])
        # pass 1: slice per-group blocks and refresh latest EU values, so
        # derived channels see the freshest cross-group samples in pass 2
        blocks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for group in self.rings:
            decim = self._group_decim[group]
            mask = idx % decim == 0
            if not mask.any():
                continue
            gt = t[mask]
            gv = eu[np.ix_(mask, self._group_cols[group])]
            blocks[group] = (gt, gv)
            for tag, k in self._group_phys[group].items():
                self._latest_eu[tag] = float(gv[-1, k])
        # pass 2: append derived columns, store, notify listeners
        for group, (gt, gv) in blocks.items():
            derived = self._derived_by_group.get(group, [])
            if derived:
                phys = self._group_phys[group]
                cols = []
                for dtag, fn, refs in derived:
                    try:
                        env = {
                            r: (gv[:, phys[r]] if r in phys else self._latest_eu.get(r, np.nan))
                            for r in refs
                        }
                        arr = np.asarray(fn(env), dtype=np.float64)
                        if arr.ndim == 0:
                            arr = np.full(len(gt), float(arr))
                    except Exception:
                        log.exception("derived channel %s failed", dtag)
                        arr = np.full(len(gt), np.nan)
                    cols.append(arr)
                gv = np.column_stack([gv, *cols])
            self.rings[group].append(gt, gv)
            for listener in self._listeners:
                try:
                    listener(group, gt, gv)
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

    def frames_since(
        self,
        since: float | dict[str, float],
        tags: list[str] | None = None,
        default_t: float = 0.0,
    ) -> list[dict]:
        """Raw new samples per rate group, for WS `frame` messages.

        `since` is either a single watermark applied to every group, or a
        per-group ``{group: t_last}`` dict (with `default_t` for groups not yet
        seen). A per-group watermark is required so a slower group whose samples
        arrive a little behind the fastest group is not skipped by a single
        shared cursor that the fast group has already advanced past. Each frame
        carries its `group` so the caller can advance the right watermark.
        """
        frames = []
        for group, ring in self.rings.items():
            sel = [tag for tag in ring.tags if tags is None or tag in tags]
            if not sel:
                continue
            since_g = since.get(group, default_t) if isinstance(since, dict) else since
            t, v = ring.window(since_g)
            if len(t) == 0:
                continue
            cols = ring.columns(sel)
            frames.append(
                {
                    "type": "frame",
                    "group": group,
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

    def last_sample_age(self) -> float | None:
        """Seconds since the last block was ingested (wall clock), or None if
        nothing has arrived yet. Used to reject arming on frozen telemetry."""
        if self._last_ingest_wall is None:
            return None
        return max(0.0, time.time() - self._last_ingest_wall)

    def latest_raw(self) -> dict[str, float]:
        """Most recent raw voltage per input tag (pre-calibration)."""
        return dict(self._latest_raw)
