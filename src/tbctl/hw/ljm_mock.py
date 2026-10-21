"""Full T7 simulator implementing the DeviceInterface surface (spec §9).

Covers:
  - streamed synthetic sensor data (noise + per-tag settable setpoints)
  - streamed DIO image (FIO_EIO_STATE) with 1 ms edge timing
  - USER_RAM register file + DIO_STATE/DIO_INHIBIT semantics
  - FIFO accepting §5 payloads
  - a faithful reimplementation of the §6 Lua sequencer state machine,
    including CRC verification, heartbeat timeout and the igniter guard

The embedded "Lua" runs on virtual 1 ms ticks, advanced lazily from the
mock's clock on every device interaction — no background threads, so tests
can drive it deterministically with a fake clock. `tbctl-node --sim`
selects this device; the entire frontend and all integration tests run
against it with zero hardware. It is also the operator training mode.
"""

from __future__ import annotations

import bisect
import math
import random
import struct
import time
from collections.abc import Callable

import numpy as np

from tbctl.config.models import (
    AnalogInChannel,
    ChannelMap,
    IgniterChannel,
    LinearCal,
    PolynomialCal,
    TableCal,
    ThermocoupleChannel,
    ValveChannel,
    dio_number,
)
from tbctl.crc import crc16_ccitt_false
from tbctl.hw.registers_gen import (
    ARM_MAGIC,
    DEFAULT_HB_TIMEOUT_MS,
    HEADER_BYTES,
    INTERP_VERSION,
    MAX_STEPS,
    REG_NAME,
    STEP_BYTES,
    TRAILER_BYTES,
    Cmd,
    SeqError,
    SeqState,
)
from tbctl.services.thermocouple import type_k_emf_mv

_NAME_OF = REG_NAME  # logical -> LJM register name
DIO_SCAN_NAME = "FIO_EIO_STATE"  # see tbctl.services.acquisition


class LuaSequencerSim:
    """Transliteration of lua/sequencer.lua's state machine (§6).

    Parses the FIFO byte stream independently of tbctl.seqtable (mirroring
    what the real Lua does), recomputes the CRC over what it parsed, and
    publishes status through the mock's register file.
    """

    def __init__(self, mock: MockDevice):
        self._m = mock
        self.state = SeqState.IDLE
        self.error = SeqError.NONE
        self.n_steps = 0
        self.n_abort = 0
        self.step_t: list[int] = []
        self.step_w: list[int] = []
        self.abort_t: list[int] = []
        self.abort_w: list[int] = []
        self.dio_inhibit = 0xFFFF
        self.safe_word = 0
        self.igniter_guard_ms = 0
        self.igniter_mask = 0
        self.crc = 0
        self.cur_step = 0  # 1-based index of next step (main or abort)
        self.elapsed_ms = 0
        self.igniter_on_ms = 0
        self.hb_last_value = 0
        self.hb_last_change_ms = 0
        self.now_ms = 0
        self._publish()

    # -- register publication ---------------------------------------------

    def _publish(self) -> None:
        m = self._m
        m._regs[_NAME_OF["STATE"]] = float(self.state)
        m._regs[_NAME_OF["ERROR_CODE"]] = float(self.error)
        m._regs[_NAME_OF["STEP_COUNT"]] = float((self.n_steps << 8) | self.n_abort)
        m._regs[_NAME_OF["CRC16"]] = float(self.crc)
        m._regs[_NAME_OF["CUR_STEP"]] = float(self.cur_step)
        m._regs[_NAME_OF["ELAPSED_MS"]] = float(self.elapsed_ms)
        m._regs[_NAME_OF["INTERP_VERSION"]] = float(INTERP_VERSION)

    # -- helpers -----------------------------------------------------------

    def _apply_word(self, word: int) -> None:
        self._m.apply_dio_masked(word, self.dio_inhibit)

    def _start_abort(self, error: SeqError) -> None:
        self.state = SeqState.ABORTING
        self.error = error
        self.elapsed_ms = 0
        self.cur_step = 1
        self.igniter_on_ms = 0
        if self.n_abort == 0:
            # empty abort table (compiler forbids it, but be safe): safe word now
            self._apply_word(self.safe_word)
            self.state = SeqState.ABORTED
        self._publish()

    # -- LOAD --------------------------------------------------------------

    def _cmd_load(self) -> None:
        raw = self._m.fifo_drain()
        ok = self._parse(raw)
        if ok:
            self.state = SeqState.LOADED
            self.error = SeqError.NONE
        else:
            self.state = SeqState.FAULT
            self.error = SeqError.BAD_TABLE
            self._apply_word(self.safe_word)
        self._publish()

    def _parse(self, raw: bytes) -> bool:
        if len(raw) < HEADER_BYTES + TRAILER_BYTES:
            return False
        n_steps, n_abort, inhibit, safe, guard, mask = struct.unpack_from("<HHHHHH", raw, 0)
        if n_steps > MAX_STEPS or n_abort > MAX_STEPS:
            return False
        expect = HEADER_BYTES + (n_steps + n_abort) * STEP_BYTES + TRAILER_BYTES
        if len(raw) != expect:
            return False
        step_t, step_w, abort_t, abort_w = [], [], [], []
        off = HEADER_BYTES
        for dst_t, dst_w, count in ((step_t, step_w, n_steps), (abort_t, abort_w, n_abort)):
            prev = -1
            for _ in range(count):
                t_ms, word = struct.unpack_from("<IH", raw, off)
                off += STEP_BYTES
                if t_ms < prev:
                    return False  # non-monotonic
                prev = t_ms
                dst_t.append(t_ms)
                dst_w.append(word)
        (crc_stated,) = struct.unpack_from("<H", raw, off)
        crc_actual = crc16_ccitt_false(raw[:off])
        if crc_stated != crc_actual:
            return False
        if guard != 0 and mask == 0:
            return False
        self.n_steps, self.n_abort = n_steps, n_abort
        self.step_t, self.step_w = step_t, step_w
        self.abort_t, self.abort_w = abort_t, abort_w
        self.dio_inhibit, self.safe_word = inhibit, safe
        self.igniter_guard_ms, self.igniter_mask = guard, mask
        self.crc = crc_actual
        self.cur_step = 0
        self.elapsed_ms = 0
        return True

    # -- command handling (polled every tick; also on host CMD write) ------

    def poll_cmd(self) -> None:
        m = self._m
        cmd = int(m._regs.get(_NAME_OF["CMD"], 0))
        if cmd == Cmd.NOP:
            return
        m._regs[_NAME_OF["CMD"]] = 0.0  # consume
        if cmd == Cmd.LOAD and self.state == SeqState.IDLE:
            self._cmd_load()
        elif cmd == Cmd.ARM and self.state == SeqState.LOADED:
            if int(m._regs.get(_NAME_OF["ARM_CODE"], 0)) == ARM_MAGIC:
                m._regs[_NAME_OF["ARM_CODE"]] = 0.0
                self._apply_word(self.safe_word)  # defined start state
                self.hb_last_value = int(m._regs.get(_NAME_OF["HEARTBEAT"], 0))
                self.hb_last_change_ms = self.now_ms
                self.state = SeqState.ARMED
                self._publish()
        elif cmd == Cmd.FIRE and self.state == SeqState.ARMED:
            self.state = SeqState.RUNNING
            self.elapsed_ms = 0
            self.cur_step = 1
            self.igniter_on_ms = 0
            self._publish()
        elif cmd == Cmd.ABORT:
            if self.state == SeqState.RUNNING:
                self._start_abort(SeqError.HOST_ABORT)
            elif self.state == SeqState.ARMED:
                self._apply_word(self.safe_word)
                self.state = SeqState.ABORTED
                self.error = SeqError.HOST_ABORT
                self._publish()
        elif cmd == Cmd.RESET and self.state in (
            SeqState.LOADED,
            SeqState.DONE,
            SeqState.ABORTED,
            SeqState.FAULT,
        ):
            if self.n_steps:
                self._apply_word(self.safe_word)
            self.state = SeqState.IDLE
            self.error = SeqError.NONE
            self.cur_step = 0
            self.elapsed_ms = 0
            self._publish()

    # -- the 1 ms tick ------------------------------------------------------

    def _hb_timeout_ms(self) -> int:
        v = int(self._m._regs.get(_NAME_OF["HB_TIMEOUT_MS"], 0))
        return v if v > 0 else DEFAULT_HB_TIMEOUT_MS

    def _check_heartbeat(self) -> bool:
        hb = int(self._m._regs.get(_NAME_OF["HEARTBEAT"], 0))
        if hb != self.hb_last_value:
            self.hb_last_value = hb
            self.hb_last_change_ms = self.now_ms
        return (self.now_ms - self.hb_last_change_ms) <= self._hb_timeout_ms()

    def _track_igniter(self, applied_word: int, dt_ms: int) -> bool:
        """Returns False when the guard trips."""
        if not self.igniter_guard_ms or not self.igniter_mask:
            return True
        if applied_word & self.igniter_mask:
            self.igniter_on_ms += dt_ms
            if self.igniter_on_ms > self.igniter_guard_ms:
                # force igniter low immediately, then abort
                self._apply_word(self._m.dio_word16() & ~self.igniter_mask & 0xFFFF)
                return False
        else:
            self.igniter_on_ms = 0
        return True

    def tick(self) -> None:
        """One virtual millisecond."""
        self.now_ms += 1
        self.poll_cmd()

        if self.state == SeqState.ARMED:
            if not self._check_heartbeat():
                self._start_abort(SeqError.HB_TIMEOUT)
            return

        if self.state == SeqState.RUNNING:
            self.elapsed_ms += 1
            # catch-up semantics: apply every due step, in order
            i = self.cur_step
            while i <= self.n_steps and self.step_t[i - 1] <= self.elapsed_ms:
                self._apply_word(self.step_w[i - 1])
                i += 1
            self.cur_step = i
            if not self._track_igniter(self._m.dio_word16(), 1):
                self._start_abort(SeqError.IGNITER_GUARD)
                return
            if not self._check_heartbeat():
                self._start_abort(SeqError.HB_TIMEOUT)
                return
            if self.cur_step > self.n_steps:
                self.state = SeqState.DONE
            self._publish()
            return

        if self.state == SeqState.ABORTING:
            self.elapsed_ms += 1
            i = self.cur_step
            while i <= self.n_abort and self.abort_t[i - 1] <= self.elapsed_ms:
                self._apply_word(self.abort_w[i - 1])
                i += 1
            self.cur_step = i
            if self.cur_step > self.n_abort:
                self._apply_word(self.safe_word)  # hold safe word
                self.state = SeqState.ABORTED
            self._publish()
            return


class MockDevice:
    """Simulated T7 (spec §9). Same synchronous surface as LjmDevice."""

    MAX_CATCHUP_MS = 10_000

    def __init__(
        self,
        chmap: ChannelMap,
        clock: Callable[[], float] | None = None,
        seed: int = 1234,
    ):
        self._chmap = chmap
        self._clock = clock or time.monotonic
        self._rng = random.Random(seed)
        self._open = False
        self._regs: dict[str, float] = {}
        self._dio_state = 0  # u32 electrical image
        # DIO word history (time, word16) so streamed FIO_EIO_STATE samples
        # see edges at their 1 ms tick time, not at block read time
        self._dio_hist_t: list[float] = []
        self._dio_hist_w: list[int] = []
        self._tick_at: float | None = None  # virtual time of the tick in progress
        self._dio_inhibit_reg = 0  # value of the DIO_INHIBIT register
        self._fifo = bytearray()
        self._setpoints_eu: dict[str, float] = {}
        self._noise_sigma_eu: dict[str, float] = {}
        self._by_hw = {c.hw: c for c in chmap.channels if c.kind != "derived"}
        # stream state
        self._scan_names: list[str] = []
        self._scan_rate = 0.0
        self._scans_per_read = 0
        self._stream_t0 = 0.0
        self._stream_idx = 0
        # embedded sequencer
        self._lua = LuaSequencerSim(self)
        self._last_advance = self._clock()
        # defaults per channel kind
        for c in chmap.inputs():
            if isinstance(c, ThermocoupleChannel):
                self._setpoints_eu[c.tag] = 20.0
                self._noise_sigma_eu[c.tag] = 0.3
            else:
                self._setpoints_eu[c.tag] = 0.0
                self._noise_sigma_eu[c.tag] = 0.02
        # apply hardware fail-safe image at power-on
        self.apply_dio_masked(chmap.safe_word(), chmap.dio_inhibit())
        self._regs["TEMPERATURE_DEVICE_K"] = 298.15
        self._regs["SERIAL_NUMBER"] = 470000001.0  # sim identity
        self._regs["FIRMWARE_VERSION"] = 1.0332

    # ------------------------------------------------------------------ sim
    def sim_set(self, tag: str, value_eu: float, noise_sigma_eu: float | None = None) -> None:
        """Set the synthetic setpoint (engineering units) for an input tag."""
        self._chmap.by_tag(tag)  # raises on unknown
        self._setpoints_eu[tag] = value_eu
        if noise_sigma_eu is not None:
            self._noise_sigma_eu[tag] = noise_sigma_eu

    def sim_setpoints(self) -> dict[str, float]:
        return dict(self._setpoints_eu)

    @property
    def lua(self) -> LuaSequencerSim:
        return self._lua

    def advance(self) -> None:
        """Advance the embedded Lua to 'now' in 1 ms virtual ticks."""
        now = self._clock()
        dt_ms = int((now - self._last_advance) * 1000.0)
        if dt_ms <= 0:
            return
        dt_ms = min(dt_ms, self.MAX_CATCHUP_MS)
        start, self._last_advance = self._last_advance, now
        try:
            for k in range(dt_ms):
                self._tick_at = start + (k + 1) / 1000.0
                self._lua.tick()
        finally:
            self._tick_at = None

    # ----------------------------------------------------------- DIO image
    def apply_dio_masked(self, word16: int, inhibit16: int) -> None:
        keep = self._dio_state & (0xFFFF0000 | (inhibit16 & 0xFFFF))
        self._dio_state = keep | (word16 & ~inhibit16 & 0xFFFF)
        self._note_dio()

    def dio_word16(self) -> int:
        return self._dio_state & 0xFFFF

    def _note_dio(self) -> None:
        w = self.dio_word16()
        if self._dio_hist_w and self._dio_hist_w[-1] == w:
            return
        self._dio_hist_t.append(self._clock() if self._tick_at is None else self._tick_at)
        self._dio_hist_w.append(w)
        if len(self._dio_hist_t) > 10_000:  # bounded; stream reads are ~0.1 s behind
            del self._dio_hist_t[:5_000], self._dio_hist_w[:5_000]

    def dio_word_at(self, t: float) -> int:
        """DIO word (16-bit) as it was at mock-clock time t."""
        i = bisect.bisect_right(self._dio_hist_t, t) - 1
        return self._dio_hist_w[max(i, 0)] if self._dio_hist_w else self.dio_word16()

    # ------------------------------------------------------------ registers
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def connected(self) -> bool:
        return self._open

    def _volts_for(self, ch: AnalogInChannel | ThermocoupleChannel, t: float) -> float:
        eu = self._setpoints_eu[ch.tag]
        # slow sine wobble for chart liveliness + gaussian noise
        eu = eu + 0.01 * max(abs(eu), 1.0) * math.sin(2 * math.pi * 0.1 * t + hash(ch.tag) % 7)
        eu += self._rng.gauss(0.0, self._noise_sigma_eu[ch.tag])
        if isinstance(ch, ThermocoupleChannel):
            cj_c = self._regs.get("TEMPERATURE_DEVICE_K", 298.15) - 273.15
            mv = type_k_emf_mv(eu) - type_k_emf_mv(cj_c)
            return mv / 1000.0
        cal = ch.cal
        if isinstance(cal, LinearCal):
            return (eu - cal.offset) / cal.gain if cal.gain else 0.0
        if isinstance(cal, PolynomialCal):
            if len(cal.coeffs) >= 2 and cal.coeffs[1]:
                return (eu - cal.coeffs[0]) / cal.coeffs[1]
            return eu
        if isinstance(cal, TableCal):
            return float(np.interp(eu, cal.y, cal.x))
        return eu

    def read_name(self, name: str) -> float:
        self.advance()
        if name in self._by_hw and isinstance(self._by_hw[name], ValveChannel | IgniterChannel):
            return float((self._dio_state >> dio_number(name)) & 1)
        if name == "DIO_STATE":
            return float(self._dio_state)
        if name == "DIO_INHIBIT":
            return float(self._dio_inhibit_reg)
        if name == _NAME_OF["FIFO_BYTES_IN"]:
            return float(len(self._fifo))
        if name in self._by_hw:  # direct AIN poll
            return self._volts_for(self._by_hw[name], self._clock())
        return self._regs.get(name, 0.0)

    def read_names(self, names: list[str]) -> list[float]:
        return [self.read_name(n) for n in names]

    def write_name(self, name: str, value: float) -> None:
        self.advance()
        if name in self._by_hw and isinstance(self._by_hw[name], ValveChannel | IgniterChannel):
            bit = dio_number(name)
            if value:
                self._dio_state |= 1 << bit
            else:
                self._dio_state &= ~(1 << bit)
            self._note_dio()
            return
        if name == "DIO_INHIBIT":
            self._dio_inhibit_reg = int(value)
            return
        if name == "DIO_STATE":
            self.apply_dio_masked(int(value) & 0xFFFF, self._dio_inhibit_reg & 0xFFFF)
            return
        self._regs[name] = float(value)
        if name == _NAME_OF["CMD"]:
            # the real Lua consumes CMD on its next 1 ms tick; processing it
            # here keeps the lazily-ticked sim host-observably identical
            self._lua.poll_cmd()

    def write_names(self, names: list[str], values: list[float]) -> None:
        for n, v in zip(names, values, strict=True):
            self.write_name(n, v)

    def write_name_array(self, name: str, values: list[int]) -> None:
        self.advance()
        if name != _NAME_OF["FIFO_DATA"]:
            raise ValueError(f"array write only supported for FIFO data, got {name}")
        for w in values:
            self._fifo += int(w).to_bytes(2, "little")

    def write_name_bytes(self, name: str, data: bytes) -> None:
        # Real hardware uses this for LUA_SOURCE_WRITE; the mock's Lua is
        # embedded, so accept and ignore script uploads.
        self.advance()
        if name != "LUA_SOURCE_WRITE":
            raise ValueError(f"byte write only supported for LUA_SOURCE_WRITE, got {name}")

    def read_name_bytes(self, name: str, num_bytes: int) -> bytes:
        self.advance()
        if name != "LUA_DEBUG_DATA":
            raise ValueError(f"byte read only supported for LUA_DEBUG_DATA, got {name}")
        return b""

    def reboot(self, delay_ticks: int = 40) -> None:
        return

    def fifo_drain(self) -> bytes:
        raw = bytes(self._fifo)
        self._fifo.clear()
        return raw

    # ------------------------------------------------------------ streaming
    def stream_start(
        self, scan_names: list[str], scan_rate_hz: float, scans_per_read: int
    ) -> float:
        for n in scan_names:
            if n not in self._by_hw and n != DIO_SCAN_NAME:
                raise ValueError(f"cannot stream unknown hw {n!r}")
        self._scan_names = list(scan_names)
        self._scan_rate = float(scan_rate_hz)
        self._scans_per_read = scans_per_read
        self._stream_t0 = self._clock()
        self._stream_idx = 0
        return self._scan_rate

    def stream_read(self) -> tuple[np.ndarray, int]:
        """Blocking, like eStreamRead: waits until scans_per_read scans exist."""
        if not self._scan_names:
            raise RuntimeError("stream not started")
        n = self._scans_per_read
        target = (self._stream_idx + n) / self._scan_rate
        wait = self._stream_t0 + target - self._clock()
        if wait > 0:
            time.sleep(wait)
        self.advance()
        block = np.empty((n, len(self._scan_names)), dtype=np.float64)
        for i in range(n):
            t = self._stream_t0 + (self._stream_idx + i) / self._scan_rate
            for j, name in enumerate(self._scan_names):
                if name == DIO_SCAN_NAME:
                    block[i, j] = float(self.dio_word_at(t))
                else:
                    block[i, j] = self._volts_for(self._by_hw[name], t)
        self._stream_idx += n
        # backlog: scans generated but not yet fetched beyond this block
        avail = int((self._clock() - self._stream_t0) * self._scan_rate) - self._stream_idx
        return block, max(0, avail)

    def stream_stop(self) -> None:
        self._scan_names = []
