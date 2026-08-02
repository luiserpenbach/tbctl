"""Sequencer host side (spec §3): compile, upload, verify, arm/fire/abort.

Python never executes the sequence — the T7 Lua interpreter does (D2). This
service compiles YAML to the §5 binary table, uploads it over the FIFO,
verifies by CRC + step-count read-back, gates ARM behind preconditions and
control mode, publishes the heartbeat while armed/running, and mirrors
status registers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from tbctl.config.loader import ConfigBundle
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

        from tbctl.services.redlines import RedlineWatcher

        async def _redline_abort(reason: str) -> None:
            log.error("aborting sequence: %s", reason)
            await self.abort()

        self.redlines = RedlineWatcher(acq, events, _redline_abort)

    # -- register helpers --------------------------------------------------

    def _read(self, logical: str) -> int:
        return int(self._iface.read_name(REG_NAME[logical]))

    def _write(self, logical: str, value: int) -> None:
        self._iface.write_name(REG_NAME[logical], float(value))

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

        # verification: device recomputed CRC over what it parsed (D8/§5)
        dev_crc = await asyncio.to_thread(self._read, "CRC16")
        dev_counts = await asyncio.to_thread(self._read, "STEP_COUNT")
        want_counts = (len(compiled.table.steps) << 8) | len(compiled.table.abort)
        crc_ok = dev_crc == compiled.table.crc16 and dev_counts == want_counts
        if not crc_ok:
            self._events.log(
                "seq_verify_failed",
                sequence=name,
                host_crc=compiled.table.crc16,
                device_crc=dev_crc,
            )
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
        values = self._acq.latest_values()
        failures = []
        for pre in seq.preconditions:
            v = values.get(pre.tag)
            if v is None:
                failures.append(f"{pre.tag}: no live data")
                continue
            if pre.min is not None and v < pre.min:
                failures.append(f"{pre.tag}: {v:.2f} < min {pre.min}")
            if pre.max is not None and v > pre.max:
                failures.append(f"{pre.tag}: {v:.2f} > max {pre.max}")
        return failures

    async def arm(self, confirm: str, control_enabled: bool) -> None:
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
        self._heartbeat.start()
        try:
            await asyncio.to_thread(_arm)
            await self._await_state(SeqState.ARMED)
        except Exception:
            await self._heartbeat.stop()
            raise
        self.redlines.arm(seq_model.redlines)
        self._events.log("seq_armed", sequence=self.loaded.name, redlines=len(seq_model.redlines))

    async def fire(self, confirm: str) -> None:
        if self.loaded is None:
            raise SequencerError("no sequence loaded")
        if confirm != self.loaded.name:
            raise SequencerError(f"confirm mismatch: expected {self.loaded.name!r}")
        st = await asyncio.to_thread(self.state)
        if st != SeqState.ARMED:
            raise SequencerError(f"cannot fire from {st.name}")
        await asyncio.to_thread(self._write, "CMD", Cmd.FIRE)
        await self._await_state(SeqState.RUNNING, SeqState.DONE)
        self._events.log("seq_fired", sequence=self.loaded.name)

    async def abort(self) -> None:
        """ALWAYS accepted; the fastest path (§7). No state checks first."""
        await asyncio.to_thread(self._write, "CMD", Cmd.ABORT)
        self._events.log("seq_abort_commanded")

    async def reset(self) -> None:
        st = await asyncio.to_thread(self.state)
        if st in (SeqState.RUNNING, SeqState.ABORTING):
            raise SequencerError(f"cannot reset while {st.name}")
        await self._heartbeat.stop()
        self.redlines.disarm()
        if st != SeqState.IDLE:
            await asyncio.to_thread(self._write, "CMD", Cmd.RESET)
            await self._await_state(SeqState.IDLE)
        self._events.log("seq_reset")

    async def watch_terminal(self) -> None:
        """Stop the heartbeat once the sequence reaches a terminal state.

        Called from the WS status loop cadence; cheap no-op otherwise.
        """
        if self._heartbeat.running:
            st = await asyncio.to_thread(self.state)
            if st in (SeqState.DONE, SeqState.ABORTED, SeqState.FAULT, SeqState.IDLE):
                await self._heartbeat.stop()
                self.redlines.disarm()
                if st in (SeqState.ABORTED, SeqState.FAULT):
                    self._events.log("seq_terminal", state=st.name, error=self.error().name)

    async def shutdown(self) -> None:
        await self._heartbeat.stop()
        self.redlines.disarm()
