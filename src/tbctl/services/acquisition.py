"""Acquisition: stream config, EU conversion, ring buffers (spec §3).

One T7 stream runs at the fastest rate any configured channel needs (when a
channel averages: the first of AVERAGE_STREAM_RATES_HZ the T7 accepts); slower
rate groups are decimated by an integer factor into their own ring buffers,
either keeping one scan per sample period or, for `average: true` channels,
the mean of all of them.
How fast the T7 can scan depends on the inputs: each +-0.1 V input needs long
settling, and on the bench T7 three of them limit the scan list to 100 Hz. A
faster group with such inputs in the same stream fails at start
(STREAM_SCAN_OVERLAP); only averaging falls back to a slower rate by itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

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
from tbctl.hw.ljm_iface import DeviceError, DeviceInterface, StreamRateError
from tbctl.services.thermocouple import (
    T7_TEMP_SENSOR_AIN,
    compensated_temp_degc,
    t7_temp_sensor_degc,
)

log = logging.getLogger(__name__)

STREAM_READS_HZ = 40  # stream block rate (scans_per_read = scan rate / this)
STREAM_POLL_S = 0.005  # wait between reads that returned no scans yet
RESTART_MAX_DELAY_S = 5.0  # back-off cap between stream restart attempts
# The T7 hardware watchdog only counts host commands, not stream data. With no
# dashboard connected nothing else talks to the device, the watchdog fires
# after watchdog_timeout_s and the stream stalls (bench: every ~9 s).
WATCHDOG_FEED_S = 1.0
WATCHDOG_FEED_REG = "DIO_STATE"  # any cheap read that works while streaming
# Streamed DIO image covering DIO 0-15, i.e. every possible sequencer output
# (FIO0-7 + EIO0-7). Read-only and does not change line direction.
DIO_SCAN_NAME = "FIO_EIO_STATE"


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

    def _segments(self) -> list[tuple[int, int]]:
        """Physical [start, stop) slices of the ring in time order. Reads index
        these directly instead of copying the whole ring into order."""
        if self._count < self.capacity:
            return [(0, self._count)]
        return [(self._write, self.capacity), (0, self._write)]

    def window(self, t0: float, t1: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        """Samples with t0 < t <= t1 (copies), oldest first. Cost is in the
        samples returned, not the ring size."""
        ts, vs = [], []
        for start, stop in self._segments():
            seg = self._t[start:stop]
            lo = int(np.searchsorted(seg, t0, side="right"))
            hi = len(seg) if t1 is None else int(np.searchsorted(seg, t1, side="right"))
            if hi > lo:
                ts.append(self._t[start + lo : start + hi])
                vs.append(self._v[start + lo : start + hi])
        if not ts:
            return np.empty(0), np.empty((0, len(self.tags)))
        if len(ts) == 1:
            return ts[0].copy(), vs[0].copy()
        return np.concatenate(ts), np.concatenate(vs)

    def latest(self) -> dict[str, float]:
        if self._count == 0:
            return {}
        row = self._v[(self._write - 1) % self.capacity]
        return {tag: float(row[i]) for tag, i in self._col.items()}

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
        self._epoch_sample = 0  # _samples_in at _stream_epoch (moves on restart)
        self._samples_in = 0
        self.dropped_scans = 0
        self._capacity_s = capacity_s
        # listeners get (group_name, t_epoch, eu_block) after every ingest;
        # survive reconfigure() so the recorder hook stays attached
        self._listeners: list[Callable[[str, np.ndarray, np.ndarray], None]] = []
        # awaited with (gap_start, gap_end) epoch seconds after a stream restart
        self._restart_hooks: list[Callable[[float, float], Awaitable[None]]] = []
        # False from a failed stream read until the stream is restarted
        self.stream_ok = True
        self.stream_error: str | None = None
        self._fed_at = 0.0  # monotonic time of the last watchdog feed
        self._configure(chmap, capacity_s)

    def _configure(self, chmap: ChannelMap, capacity_s: float) -> None:
        self._chmap = chmap
        inputs = chmap.inputs()
        self._scan_channels = inputs  # scan list order == channel order
        self._scan_names = [c.hw for c in inputs]
        self._converters = [_make_converter(c) for c in inputs]
        # fastest first; more than one only when a channel averages
        self._stream_rates = chmap.stream_rate_candidates()
        self._rate_idx = 0
        self.scan_rate_hz = self._stream_rates[0]
        # outputs: one extra scan entry (the DIO word), split into a 0/1
        # "open/on" column per output, recorded in the fastest input group
        self._outputs = chmap.outputs() if chmap.device.record_outputs and inputs else []
        self._output_bits = [(chmap.bit_index(c.tag), chmap.active_level(c)) for c in self._outputs]
        output_group = (
            max(
                {c.rate_group for c in inputs},
                key=lambda g: chmap.rate_groups[g].rate_hz,
            )
            if self._outputs
            else None
        )
        if self._outputs:
            self._scan_names.append(DIO_SCAN_NAME)
        # cold junction: stream the T7 temperature sensor, last scan entry
        self._cj_col: int | None = None
        if any(isinstance(c, ThermocoupleChannel) for c in inputs):
            if T7_TEMP_SENSOR_AIN not in self._scan_names:
                self._scan_names.append(T7_TEMP_SENSOR_AIN)
            self._cj_col = self._scan_names.index(T7_TEMP_SENSOR_AIN)
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
        # averaged channels: positions within the group block, and the volts
        # of the scans since the last completed window (carried across reads)
        self._group_avg: dict[str, list[int]] = {}
        self._avg_carry: dict[str, np.ndarray] = {}
        self._group_rate: dict[str, float] = {}
        for group, rg in chmap.rate_groups.items():
            tags = [c.tag for c in inputs if c.rate_group == group]
            otags = [c.tag for c in self._outputs] if group == output_group else []
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
            self._group_rate[group] = rg.rate_hz
            self._group_cols[group] = [self._scan_names.index(chmap.by_tag(t).hw) for t in tags]
            self._group_phys[group] = {t: k for k, t in enumerate(tags)}
            self._group_avg[group] = [k for k, t in enumerate(tags) if chmap.by_tag(t).average]
            self._avg_carry[group] = np.empty((0, len(self._group_avg[group])))
            self.rings[group] = RingBuffer(tags + otags + dtags, rg.rate_hz, capacity_s)
        self._output_group = output_group
        self._tag_group = (
            {c.tag: c.rate_group for c in inputs}
            | {c.tag: output_group for c in self._outputs}
            | derived_group_of
        )
        # latest EU value per physical tag, for cross-group sample-and-hold
        self._latest_eu: dict[str, float] = {}
        self._cj_degc = 25.0
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

    def add_restart_hook(self, fn: Callable[[float, float], Awaitable[None]]) -> None:
        """`await fn(gap_start, gap_end)` after the stream recovers from a failure."""
        self._restart_hooks.append(fn)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        self._samples_in = 0
        self._epoch_sample = 0
        self._clear_avg_carry()
        await self._start_stream()
        self._stream_epoch = time.time()
        self.stream_ok, self.stream_error = True, None
        self._task = asyncio.create_task(self._run(), name="acquisition")

    async def _start_stream(self, first: int = 0) -> None:
        """Start the stream at the first rate from `_stream_rates[first:]` the
        device accepts (a restart resumes from the rate that worked)."""
        # per-channel range + single-ended/differential, before the stream
        # starts (stream mode honours both); the T7 defaults are +-10 V vs GND,
        # and a power-cycled T7 is back at those defaults after a restart
        if settings := self._chmap.ain_settings():
            names, values = zip(*settings, strict=True)
            await asyncio.to_thread(self._iface.write_names, list(names), list(values))
        rates = self._stream_rates
        for i in range(first, len(rates)):
            rate = rates[i]
            # ~40 reads/s: 25 ms blocks keep glass-to-glass latency low
            self._scans_per_read = max(1, round(rate / STREAM_READS_HZ))
            actual = await asyncio.to_thread(
                self._iface.stream_start, self._scan_names, rate, self._scans_per_read
            )
            if len(rates) == 1:
                break
            try:
                await self._first_block()
                break
            except StreamRateError as e:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._iface.stream_stop)
                if i == len(rates) - 1:
                    raise
                log.warning(
                    "device cannot scan the list at %g Hz (%s); trying %g Hz", rate, e, rates[i + 1]
                )
        if abs(actual - rate) / rate > 0.01:
            log.warning("device stream rate %.1f != requested %.1f", actual, rate)
        self._rate_idx = i
        for group, group_rate in self._group_rate.items():
            self._group_decim[group] = round(rate / group_rate)
        self.scan_rate_hz = actual
        if len(rates) > 1:
            log.info(
                "stream at %g Hz; averaged channels use %s scans per sample",
                rate,
                ", ".join(f"{g}: {self._group_decim[g]}" for g, a in self._group_avg.items() if a),
            )

    async def _first_block(self, timeout_s: float = 1.0) -> None:
        """Wait for the first stream data: the T7 reports a scan list it cannot
        complete in one period (STREAM_SCAN_OVERLAP) there, not at start. The
        block is dropped; sample times start once the stream is confirmed."""
        t_end = time.monotonic() + timeout_s
        while time.monotonic() < t_end:
            block, _ = await asyncio.to_thread(self._iface.stream_read)
            if len(block):
                return
            await asyncio.sleep(STREAM_POLL_S)
        raise DeviceError(f"no stream data within {timeout_s:g} s")

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
            await self._feed_watchdog()
            try:
                block, backlog = await asyncio.to_thread(self._iface.stream_read)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await self._restart_stream(e)
                continue
            if len(block) == 0:
                await asyncio.sleep(STREAM_POLL_S)
                continue
            if backlog > self.scan_rate_hz:  # >1 s behind
                log.warning("stream backlog %d scans", backlog)
            self._ingest(block)

    async def _feed_watchdog(self) -> None:
        """One command per WATCHDOG_FEED_S while this loop runs: the watchdog
        then fires only when the node (or its acquisition) is dead."""
        now = time.monotonic()
        if now - self._fed_at < WATCHDOG_FEED_S:
            return
        self._fed_at = now
        with contextlib.suppress(Exception):  # a dead link shows up in stream_read
            await asyncio.to_thread(self._iface.read_name, WATCHDOG_FEED_REG)

    async def _restart_stream(self, err: Exception) -> None:
        """A stopped stream does not come back by itself: restart it, backing
        off (1, 2, 4, 5, 5... s) while the T7 is unreachable, then report the
        gap once."""
        gap_start = self._stream_epoch + (self._samples_in - self._epoch_sample) / self.scan_rate_hz
        self.stream_ok, self.stream_error = False, str(err)
        log.warning("stream read failed (%s); restarting stream", err, exc_info=True)
        attempts = 0
        while True:
            await asyncio.sleep(min(RESTART_MAX_DELAY_S, 2.0**attempts))
            attempts += 1
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._iface.stream_stop)
            try:
                await self._start_stream(self._rate_idx)
                break
            except Exception as e:
                log.debug("stream restart attempt %d failed: %s", attempts, e)
        # Sample times count from the stream start, so re-anchor them: the
        # scans lost in between become a visible gap instead of shifting every
        # later sample back. Never before the last kept sample (rings and
        # recordings need increasing time).
        self._stream_epoch = max(time.time(), gap_start)
        self._epoch_sample = self._samples_in
        self._clear_avg_carry()  # a window cut by the outage is not averaged across it
        gap_end = self._stream_epoch
        self.stream_ok, self.stream_error = True, None
        log.warning(
            "stream restarted after %.1f s (%d attempts); no data for that span",
            gap_end - gap_start,
            attempts,
        )
        for hook in list(self._restart_hooks):
            try:
                await hook(gap_start, gap_end)
            except Exception:
                log.exception("stream restart hook failed")

    # -- data path ---------------------------------------------------------

    def _clear_avg_carry(self) -> None:
        for group, avg in self._group_avg.items():
            self._avg_carry[group] = np.empty((0, len(avg)))

    def _window_means(self, group: str, volts: np.ndarray, ends: np.ndarray) -> np.ndarray:
        """Mean volts of each window ending at a True row of `ends`, including
        scans carried over from earlier reads. Skipped scans (LJM_DUMMY_VALUE)
        are left out; a window with no valid scan is NaN. The first window
        after a start or restart can be shorter than a full period."""
        buf = np.vstack([self._avg_carry[group], volts])
        valid = buf > -9000.0
        zero = np.zeros((1, buf.shape[1]))
        sums = np.vstack([zero, np.cumsum(np.where(valid, buf, 0.0), axis=0)])
        counts = np.vstack([zero, np.cumsum(valid, axis=0)])
        last = len(self._avg_carry[group]) + np.flatnonzero(ends)  # inclusive, in buf rows
        first = np.concatenate([[0], last[:-1] + 1])
        with np.errstate(invalid="ignore", divide="ignore"):
            means = (sums[last + 1] - sums[first]) / (counts[last + 1] - counts[first])
        self._avg_carry[group] = buf[last[-1] + 1 :] if len(last) else buf
        return means

    def _ingest(self, volts: np.ndarray) -> None:
        n = len(volts)
        if n == 0:
            return
        if self._cj_col is not None:
            cj_v = volts[:, self._cj_col]
            cj_v = cj_v[cj_v > -9000.0]  # drop LJM_DUMMY_VALUE (skipped scans)
            if len(cj_v):
                self._cj_degc = t7_temp_sensor_degc(float(np.mean(cj_v)))
        idx = self._samples_in + np.arange(n)
        t = self._stream_epoch + (idx - self._epoch_sample) / self.scan_rate_hz
        n_in = len(self._converters)
        for j in range(n_in):
            self._latest_raw[self._scan_channels[j].tag] = float(volts[-1, j])
        if self._outputs:
            raw = volts[:, n_in]
            # LJM fills skipped scans with -9999 (LJM_DUMMY_VALUE): no state known
            valid = np.isfinite(raw) & (raw >= 0)
            word = np.where(valid, raw, 0).astype(np.int64)
            states = np.column_stack(
                [((word >> bit) & 1) == level for bit, level in self._output_bits]
            ).astype(np.float64)
            states[~valid] = np.nan
        # pass 1: slice per-group blocks and refresh latest EU values, so
        # derived channels see the freshest cross-group samples in pass 2
        blocks: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        for group in self.rings:
            decim = self._group_decim[group]
            avg = self._group_avg[group]
            cols = self._group_cols[group]
            if avg:
                # one sample per completed window, stamped at its last scan
                mask = idx % decim == decim - 1
                means = self._window_means(group, volts[:, [cols[k] for k in avg]], mask)
            else:
                mask = idx % decim == 0
            if not mask.any():
                continue
            rows = np.flatnonzero(mask)
            gt = t[rows]
            # convert only the kept samples (the stream may run far faster)
            gv = np.empty((len(rows), len(cols)), dtype=np.float64)
            for k, col in enumerate(cols):
                v = means[:, avg.index(k)] if k in avg else volts[rows, col]
                gv[:, k] = self._converters[col](v, self._cj_degc)
            if group == self._output_group:
                gv = np.column_stack([gv, states[mask]])
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

    @property
    def device_temp_degc(self) -> float | None:
        """T7 temperature from the streamed sensor (None without thermocouples)."""
        return self._cj_degc if self._cj_col is not None else None

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
