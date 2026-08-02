"""Sequencer host side (spec §3): compile, upload, verify, arm/fire/abort.

Python never executes the sequence — the T7 Lua interpreter does (D2). This
service compiles YAML to the §5 binary table, uploads it over the FIFO,
verifies by CRC + step-count read-back, gates ARM behind preconditions and
control mode, publishes the heartbeat while armed/running, and mirrors
status registers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from dataclasses import dataclass

from tbctl.config.loader import ConfigBundle
from tbctl.config.models import dio_number
from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.hw.registers_gen import (
    ARM_MAGIC,
    DEFAULT_HB_TIMEOUT_MS,
    INTERP_VERSION,
    REG_NAME,
    Cmd,
    SeqError,
    SeqState,
)
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.compiler import CompiledSequence, compile_sequence
from tbctl.services.events import EventLog
from tbctl.services.safety import HeartbeatPublisher

log = logging.getLogger(__name__)

POLL_INTERVAL_S = 0.01
CMD_TIMEOUT_S = 2.0

# Abort is the fastest path (§7) but the CMD register is a single last-writer
# mailbox the Lua consumes one entry per tick, so a command can be lost to a
# racing write (e.g. FIRE landing after ABORT). ABORT therefore verifies the
# device actually left the firing states and re-issues, escalating to a direct
# safe-word write on the DIO lines if the interpreter never responds.
ABORT_CONFIRM_S = 0.4
ABORT_MAX_RETRIES = 4
# Preconditions and redlines must not evaluate on frozen telemetry: if the
# newest sample is older than this the stream is considered wedged.
STALE_SAMPLE_S = 1.5
# While RUNNING the device's ELAPSED_MS must keep advancing; if it stalls this
# long with the heartbeat still being published the interpreter is presumed
# dead (crash/OOM) and the outputs are forced safe out-of-band.
LIVENESS_STALL_S = 1.0

# Device states from which the sequence can no longer actuate / fire.
_SAFE_STATES = (SeqState.ABORTING, SeqState.ABORTED, SeqState.IDLE, SeqState.FAULT)


class SequencerError(Exception):
    """Refused or failed sequencer operation (maps to HTTP 409/500)."""


@dataclass
class LoadedSequence:
    name: str
    compiled: CompiledSequence
    sha256: str
    loaded_at: float
    crc_verified: bool


class SequencerService:
    def __init__(
        self,
        bundle: ConfigBundle,
        iface: DeviceInterface,
        acq: AcquisitionService,
        events: EventLog,
        hb_timeout_ms: int = DEFAULT_HB_TIMEOUT_MS,
    ):
        self._bundle = bundle
        self._iface = iface
        self._acq = acq
        self._events = events
        self._hb_timeout_ms = hb_timeout_ms
        self._heartbeat = HeartbeatPublisher(iface)
        self.loaded: LoadedSequence | None = None
        # serialize state-advancing commands (load/arm/fire/reset) so two
        # operations can never interleave writes to the single CMD mailbox.
        # abort() deliberately does NOT take this lock — it is the fastest path
        # and instead relies on verify-and-retry to win any race.
        self._op_lock = asyncio.Lock()
        self._abort_requested = False
        # liveness tracking for the interpreter-crash backstop (C2)
        self._live_elapsed = -1
        self._live_since = 0.0
        self._live_escalated = False

        from tbctl.services.redlines import RedlineWatcher

        async def _redline_abort(reason: str) -> None:
            log.error("aborting sequence: %s", reason)
            await self.abort(reason=reason)

        self.redlines = RedlineWatcher(acq, events, _redline_abort)

    # -- register helpers --------------------------------------------------

    def _read(self, logical: str) -> int:
        return int(self._iface.read_name(REG_NAME[logical]))

    def _write(self, logical: str, value: int) -> None:
        self._iface.write_name(REG_NAME[logical], float(value))

    def _force_safe_outputs(self) -> None:
        """Last-resort de-energize: write every output to its fail-safe level
        directly on its own DIO register.

        Used only when the interpreter will not honour ABORT. Individual FIO
        writes are NOT gated by DIO_INHIBIT (which masks DIO_STATE only), so
        this reaches the valves even if the Lua never programmed the inhibit
        mask (e.g. it crashed before LOAD).
        """
        chmap = self._bundle.channels.model
        safe_word = chmap.safe_word()
        for c in chmap.outputs():
            bit = dio_number(c.hw)
            level = (safe_word >> bit) & 1
            with contextlib.suppress(Exception):
                self._iface.write_name(c.hw, float(level))

    def state(self) -> SeqState:
        return SeqState(self._read("STATE"))

    def error(self) -> SeqError:
        return SeqError(self._read("ERROR_CODE"))

    async def _await_state(self, *want: SeqState, timeout_s: float = CMD_TIMEOUT_S) -> SeqState:
        deadline = time.monotonic() + timeout_s
        while True:
            st = await asyncio.to_thread(self.state)
            if st in want:
                return st
            if time.monotonic() > deadline:
                raise SequencerError(
                    f"timeout waiting for {'/'.join(w.name for w in want)}, state={st.name}"
                )
            await asyncio.sleep(POLL_INTERVAL_S)

    # -- operations --------------------------------------------------------

    def check_interp_version(self) -> None:
        v = self._read("INTERP_VERSION")
        if v != INTERP_VERSION:
            raise SequencerError(
                f"interpreter version mismatch: device reports {v}, host expects {INTERP_VERSION}"
            )

    async def load(self, name: str) -> dict:
        """Compile + upload + CRC-verify. Returns the compile report (§7)."""
        if name not in self._bundle.sequences:
            raise SequencerError(f"unknown sequence {name!r}")
        async with self._op_lock:
            return await self._load_locked(name)

    async def _reset_device_to_idle(self) -> None:
        """Best-effort return the device to IDLE (used to recover from a failed
        load so the device never holds an unverified table)."""
        with contextlib.suppress(Exception):
            st = await asyncio.to_thread(self.state)
            if st != SeqState.IDLE:
                await asyncio.to_thread(self._write, "CMD", Cmd.RESET)
                await self._await_state(SeqState.IDLE, timeout_s=CMD_TIMEOUT_S)

    async def _load_locked(self, name: str) -> dict:
        st = await asyncio.to_thread(self.state)
        if st not in (
            SeqState.IDLE,
            SeqState.LOADED,
            SeqState.DONE,
            SeqState.ABORTED,
            SeqState.FAULT,
        ):
            raise SequencerError(f"cannot load while {st.name}")
        await asyncio.to_thread(self.check_interp_version)

        loaded_file = self._bundle.sequences[name]
        compiled = compile_sequence(loaded_file.model, self._bundle.channels.model)

        # Invalidate the loaded record BEFORE touching the device: from here
        # until verification succeeds there is no trustworthy host↔device
        # binding, so a mid-upload failure must never leave a stale `loaded`
        # (which arm() would happily confirm against the wrong device table).
        self.loaded = None

        def _upload() -> None:
            if self.state() != SeqState.IDLE:
                self._write("CMD", Cmd.RESET)
            # Allocate FIFO for the upload only; Lua frees it after PARSE so the
            # T7 shared heap can hold the stream buffer alongside the sequencer.
            from tbctl.hw.registers_gen import FIFO_ALLOCATE_BYTES

            self._iface.write_name(REG_NAME["FIFO_ALLOCATE"], float(FIFO_ALLOCATE_BYTES))
            self._iface.write_name_array(REG_NAME["FIFO_DATA"], compiled.table.to_u16_words())

        await asyncio.to_thread(_upload)
        # RESET (if sent) is consumed on the next tick; wait for IDLE then LOAD
        await self._await_state(SeqState.IDLE)
        await asyncio.to_thread(self._write, "CMD", Cmd.LOAD)
        # full 128+128 tables take the T7 ~a second to stream-parse + CRC
        st = await self._await_state(SeqState.LOADED, SeqState.FAULT, timeout_s=10.0)
        if st == SeqState.FAULT:
            self._events.log("seq_load_failed", sequence=name, error=self.error().name)
            raise SequencerError(f"device rejected table: {self.error().name}")

        # verification: device recomputed CRC over what it parsed (D8/§5). Any
        # failure here (bad CRC, or an exception reading it back) resets the
        # device so it cannot be left LOADED with an unverified table.
        try:
            dev_crc = await asyncio.to_thread(self._read, "CRC16")
            dev_counts = await asyncio.to_thread(self._read, "STEP_COUNT")
        except Exception:
            await self._reset_device_to_idle()
            raise
        want_counts = (len(compiled.table.steps) << 8) | len(compiled.table.abort)
        crc_ok = dev_crc == compiled.table.crc16 and dev_counts == want_counts
        if not crc_ok:
            self._events.log(
                "seq_verify_failed",
                sequence=name,
                host_crc=compiled.table.crc16,
                device_crc=dev_crc,
            )
            await self._reset_device_to_idle()
            raise SequencerError(
                f"table verification failed: host crc 0x{compiled.table.crc16:04X} "
                f"device crc 0x{dev_crc:04X}"
            )

        self.loaded = LoadedSequence(name, compiled, loaded_file.sha256, time.time(), True)
        self._events.log("seq_loaded", sequence=name, crc=f"0x{dev_crc:04X}")
        report = compiled.report()
        report["crc_match"] = True
        report["sha256"] = loaded_file.sha256
        report["redlines"] = [
            {"tag": r.tag, "min": r.min, "max": r.max, "persist_ms": r.persist_ms}
            for r in loaded_file.model.redlines
        ]
        return report

    def check_preconditions(self) -> list[str]:
        """Evaluate §4.2 preconditions from the live ring buffer. [] = pass."""
        if self.loaded is None:
            return ["no sequence loaded"]
        seq = self._bundle.sequences[self.loaded.name].model
        # Refuse to arm on frozen telemetry: a wedged stream keeps serving the
        # last pre-stall sample, which would let preconditions pass on data
        # that is minutes old.
        age = self._acq.last_sample_age()
        if seq.preconditions and (age is None or age > STALE_SAMPLE_S):
            shown = "no samples yet" if age is None else f"{age:.1f}s old"
            return [f"telemetry is stale ({shown}); stream not delivering fresh data"]
        values = self._acq.latest_values()
        failures = []
        for pre in seq.preconditions:
            v = values.get(pre.tag)
            if v is None:
                failures.append(f"{pre.tag}: no live data")
                continue
            # NaN/inf compares false against every bound, which would silently
            # satisfy the precondition — treat a non-finite reading as a fail.
            if not math.isfinite(v):
                failures.append(f"{pre.tag}: non-finite reading ({v})")
                continue
            if pre.min is not None and v < pre.min:
                failures.append(f"{pre.tag}: {v:.2f} < min {pre.min}")
            if pre.max is not None and v > pre.max:
                failures.append(f"{pre.tag}: {v:.2f} > max {pre.max}")
        return failures

    async def arm(self, confirm: str, control_enabled: bool) -> None:
        async with self._op_lock:
            if self.loaded is None:
                raise SequencerError("no sequence loaded")
            if confirm != self.loaded.name:
                raise SequencerError(f"confirm mismatch: expected {self.loaded.name!r}")
            if not control_enabled:
                raise SequencerError("control mode is disabled")
            st = await asyncio.to_thread(self.state)
            if st != SeqState.LOADED:
                raise SequencerError(f"cannot arm from {st.name}")
            if failures := self.check_preconditions():
                raise SequencerError("preconditions failed: " + "; ".join(failures))

            def _arm() -> None:
                self._write("HB_TIMEOUT_MS", self._hb_timeout_ms)
                self._write("HEARTBEAT", 1)
                self._write("ARM_CODE", ARM_MAGIC)
                self._write("CMD", Cmd.ARM)

            seq_model = self._bundle.sequences[self.loaded.name].model
            self._abort_requested = False
            self._reset_liveness()
            self._heartbeat.start()
            try:
                await asyncio.to_thread(_arm)
                await self._await_state(SeqState.ARMED)
            except Exception:
                await self._heartbeat.stop()
                raise
            self.redlines.arm(seq_model.redlines)
            self._events.log(
                "seq_armed", sequence=self.loaded.name, redlines=len(seq_model.redlines)
            )

    async def fire(self, confirm: str, control_enabled: bool) -> None:
        async with self._op_lock:
            if self.loaded is None:
                raise SequencerError("no sequence loaded")
            if confirm != self.loaded.name:
                raise SequencerError(f"confirm mismatch: expected {self.loaded.name!r}")
            # FIRE requires control mode just like ARM: disabling control mode
            # is the operator's "lock out the bench" action and must block a
            # fire, not merely block re-arming.
            if not control_enabled:
                raise SequencerError("control mode is disabled")
            # If an abort has been commanded (operator or redline) do not race a
            # FIRE onto the shared CMD mailbox behind it.
            if self._abort_requested:
                raise SequencerError("abort in progress; fire refused")
            st = await asyncio.to_thread(self.state)
            if st != SeqState.ARMED:
                raise SequencerError(f"cannot fire from {st.name}")
            self._reset_liveness()
            await asyncio.to_thread(self._write, "CMD", Cmd.FIRE)
            await self._await_state(SeqState.RUNNING, SeqState.DONE)
            self._events.log("seq_fired", sequence=self.loaded.name)

    async def abort(self, reason: str = "operator") -> dict:
        """ALWAYS accepted; the fastest path (§7). No state checks first.

        Verifies the device actually leaves the firing states and re-issues the
        command if the single CMD mailbox dropped it; if the interpreter never
        responds (crash/OOM) the outputs are forced safe out-of-band and the
        heartbeat is stopped so the hardware watchdog can also engage. Returns
        {confirmed, escalated, state} so callers/UI report the real outcome
        instead of an unconditional ok.
        """
        self._abort_requested = True
        confirmed = False
        final = None
        for attempt in range(ABORT_MAX_RETRIES):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._write, "CMD", Cmd.ABORT)
            try:
                final = await self._await_state(*_SAFE_STATES, timeout_s=ABORT_CONFIRM_S)
                confirmed = True
                break
            except SequencerError:
                log.warning("abort not acknowledged (attempt %d); re-issuing", attempt + 1)
        escalated = False
        if not confirmed:
            # The interpreter is not honouring ABORT — drive the valves safe
            # directly and stop feeding the heartbeat so the T7 hardware
            # watchdog (layer 2) can force the fail-safe image too.
            escalated = True
            log.critical("ABORT not acknowledged by device — forcing outputs safe out-of-band")
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._force_safe_outputs)
            await self._heartbeat.stop()
            self.redlines.disarm()
            with contextlib.suppress(Exception):
                final = await asyncio.to_thread(self.state)
        self._events.log(
            "seq_abort_commanded",
            reason=reason,
            confirmed=confirmed,
            escalated=escalated,
            state=final.name if final else "unknown",
        )
        return {
            "confirmed": confirmed,
            "escalated": escalated,
            "state": final.name if final else "unknown",
        }

    async def reset(self) -> None:
        async with self._op_lock:
            st = await asyncio.to_thread(self.state)
            if st in (SeqState.RUNNING, SeqState.ABORTING):
                raise SequencerError(f"cannot reset while {st.name}")
            # Send RESET while the heartbeat is still alive: from ARMED the Lua
            # honours RESET as a clean stand-down to IDLE, but if we stopped the
            # heartbeat first the device would instead abort on HB timeout.
            if st != SeqState.IDLE:
                await asyncio.to_thread(self._write, "CMD", Cmd.RESET)
                await self._await_state(SeqState.IDLE)
            await self._heartbeat.stop()
            self.redlines.disarm()
            self._abort_requested = False
            self._events.log("seq_reset")

    def _reset_liveness(self) -> None:
        self._live_elapsed = -1
        self._live_since = time.monotonic()
        self._live_escalated = False

    async def watch_terminal(self) -> None:
        """Stop the heartbeat once the sequence reaches a terminal state, and
        back-stop a dead interpreter while RUNNING.

        Called from the WS status loop cadence; cheap no-op otherwise.
        """
        if not self._heartbeat.running:
            return
        st = await asyncio.to_thread(self.state)
        if st in (SeqState.DONE, SeqState.ABORTED, SeqState.FAULT, SeqState.IDLE):
            await self._heartbeat.stop()
            self.redlines.disarm()
            self._abort_requested = False
            if st in (SeqState.ABORTED, SeqState.FAULT):
                self._events.log("seq_terminal", state=st.name, error=self.error().name)
            return
        if st == SeqState.RUNNING:
            await self._check_liveness()

    async def _check_liveness(self) -> None:
        """While RUNNING, ELAPSED_MS must keep advancing. If it freezes while
        we are still publishing the heartbeat, the interpreter is presumed dead
        (the T7 heartbeat/watchdog layers cannot see a stall because the host is
        still communicating) — force the outputs safe out-of-band, once."""
        if self._live_escalated:
            return
        with contextlib.suppress(Exception):
            elapsed = await asyncio.to_thread(self._read, "ELAPSED_MS")
            now = time.monotonic()
            if elapsed != self._live_elapsed:
                self._live_elapsed = elapsed
                self._live_since = now
            elif now - self._live_since > LIVENESS_STALL_S:
                self._live_escalated = True
                log.critical(
                    "interpreter appears stalled (ELAPSED_MS frozen at %d for %.1fs while "
                    "RUNNING) — forcing outputs safe",
                    elapsed,
                    now - self._live_since,
                )
                self._events.log("seq_interp_stall", elapsed_ms=int(elapsed))
                await self.abort(reason="interpreter stall")

    async def shutdown(self) -> None:
        await self._heartbeat.stop()
        self.redlines.disarm()
