"""Thin device-interface layer over LabJack LJM (spec §3 hw/ljm_iface.py).

`DeviceInterface` is the surface the rest of the node programs against;
`LjmDevice` is the real-hardware implementation and `ljm_mock.MockDevice`
the simulator (§9). All methods are synchronous and blocking, mirroring
LJM's own model — async services call them via `asyncio.to_thread`.

Register names are always symbolic LJM names (resolved to addresses inside
LJM from ljm_constants.json); tbctl logical slots come from
`tbctl.hw.registers_gen.REG_NAME` (spec D10).
"""

from __future__ import annotations

import contextlib
import threading
from typing import Protocol

import numpy as np


class DeviceError(Exception):
    """Device communication failure."""


class DeviceInterface(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    @property
    def connected(self) -> bool: ...

    # -- register access (symbolic LJM names) ------------------------------

    def read_name(self, name: str) -> float: ...

    def read_names(self, names: list[str]) -> list[float]: ...

    def write_name(self, name: str, value: float) -> None: ...

    def write_names(self, names: list[str], values: list[float]) -> None: ...

    def write_name_array(self, name: str, values: list[int]) -> None:
        """Array write to a single (FIFO) register, e.g. the step-table upload."""
        ...

    def write_name_bytes(self, name: str, data: bytes) -> None:
        """Byte-buffer write (e.g. LUA_SOURCE_WRITE). Not the same as write_name_array."""
        ...

    def read_name_bytes(self, name: str, num_bytes: int) -> bytes:
        """Byte-buffer read (e.g. LUA_DEBUG_DATA)."""
        ...

    def reboot(self, delay_ticks: int = 40) -> None:
        """Soft-reboot device and reopen the connection."""
        ...

    # -- streaming ---------------------------------------------------------

    def stream_start(
        self, scan_names: list[str], scan_rate_hz: float, scans_per_read: int
    ) -> float:
        """Start streaming; returns the actual device scan rate."""
        ...

    def stream_read(self) -> tuple[np.ndarray, int]:
        """Blocking read of one block: (scans x channels) float64, device backlog.

        Blocks until scans_per_read scans are available.
        """
        ...

    def stream_stop(self) -> None: ...


class LjmDevice:
    """Real T7 over the LJM library. Import of `labjack.ljm` is deferred so
    the package is importable (and the simulator usable) without the LJM
    shared library installed."""

    def __init__(self, identifier: str, connection: str = "ETHERNET"):
        self._identifier = identifier
        self._connection = connection
        self._handle: int | None = None
        self._ljm = None
        self._scan_names: list[str] = []
        self._scans_per_read = 0
        # On USB, stream reads and command-response (status, cold-junction)
        # from different threads collide and the T7 stops the stream
        # (LJME_NO_RESPONSE_BYTES_RECEIVED), so every call is serialised
        # there. Not on Ethernet: the lock would make the heartbeat and abort
        # wait for a whole stream block, and a stalled read would hold them.
        # `_io` is captured by each `with`, so open() can swap it safely.
        self._lock = threading.RLock()
        self._io: contextlib.AbstractContextManager = contextlib.nullcontext()

    def open(self) -> None:
        from labjack import ljm  # deferred: needs LJM shared library

        with self._lock:
            self._ljm = ljm
            self._handle = ljm.openS("T7", self._connection, self._identifier)
            try:
                usb = ljm.getHandleInfo(self._handle)[1] == ljm.constants.ctUSB
            except Exception:
                usb = True  # unknown transport: serialise, the safe side
            self._io = self._lock if usb else contextlib.nullcontext()

    def close(self) -> None:
        with self._lock:
            if self._handle is not None:
                try:
                    self._ljm.close(self._handle)
                finally:
                    self._handle = None

    @property
    def connected(self) -> bool:
        return self._handle is not None

    @property
    def serialised(self) -> bool:
        """True when calls are serialised (USB)."""
        return self._io is self._lock

    def _h(self) -> int:
        if self._handle is None:
            raise DeviceError("device not open")
        return self._handle

    def read_name(self, name: str) -> float:
        with self._io:
            return self._ljm.eReadName(self._h(), name)

    def read_names(self, names: list[str]) -> list[float]:
        with self._io:
            return list(self._ljm.eReadNames(self._h(), len(names), names))

    def write_name(self, name: str, value: float) -> None:
        with self._io:
            self._ljm.eWriteName(self._h(), name, value)

    def write_names(self, names: list[str], values: list[float]) -> None:
        with self._io:
            self._ljm.eWriteNames(self._h(), len(names), names, values)

    def write_name_array(self, name: str, values: list[int]) -> None:
        with self._io:
            self._ljm.eWriteNameArray(self._h(), name, len(values), values)

    def write_name_bytes(self, name: str, data: bytes) -> None:
        # LUA_SOURCE_WRITE (and other buffer regs) require the ByteArray API;
        # eWriteNameArray writes float register values and corrupts the payload.
        with self._io:
            self._ljm.eWriteNameByteArray(self._h(), name, len(data), data)

    def read_name_bytes(self, name: str, num_bytes: int) -> bytes:
        if num_bytes <= 0:
            return b""
        with self._io:
            raw = self._ljm.eReadNameByteArray(self._h(), name, num_bytes)
        return bytes(raw)

    def write_address_u32(self, address: int, value: int) -> None:
        with self._io:
            self._ljm.eWriteAddress(self._h(), address, self._ljm.constants.UINT32, value)

    def reboot(self, delay_ticks: int = 40) -> None:
        """Soft-reboot the T7 (SYSTEM_REBOOT = 0x4C4Axxxx) and reopen.

        Required to reclaim shared heap after failed Lua compiles. Must use the
        UINT32 address write — eWriteName float can mangle the magic prefix.
        """
        import time

        # 0x4C4A0000 | ticks; each tick is 50 ms.
        code = 0x4C4A0000 | (delay_ticks & 0xFFFF)
        try:
            self.write_address_u32(61998, code)
        finally:
            with contextlib.suppress(Exception):
                self.close()
        deadline = time.monotonic() + 15.0
        last_err: Exception | None = None
        time.sleep(max(delay_ticks, 1) * 0.05 + 0.5)
        while time.monotonic() < deadline:
            try:
                self.open()
                return
            except Exception as e:
                last_err = e
                time.sleep(0.5)
        raise DeviceError(f"device did not return after reboot: {last_err}")

    def stream_start(
        self, scan_names: list[str], scan_rate_hz: float, scans_per_read: int
    ) -> float:
        with self._io:
            addrs = self._ljm.namesToAddresses(len(scan_names), scan_names)[0]
            self._scan_names = list(scan_names)
            self._scans_per_read = scans_per_read
            # Keep the stream buffer small so it fits beside the Lua sequencer in
            # the T7's 64KB shared heap. 4096+ triggers SYSTEM_MEMORY_BEREFT with
            # the dense sequencer; 2048 is the practical ceiling on USB.
            with contextlib.suppress(Exception):
                self._ljm.eWriteName(self._h(), "STREAM_BUFFER_SIZE_BYTES", 2048)
            actual = self._ljm.eStreamStart(
                self._h(), scans_per_read, len(addrs), addrs, scan_rate_hz
            )
            return float(actual)

    def stream_read(self) -> tuple[np.ndarray, int]:
        with self._io:
            raw, device_backlog, _ljm_backlog = self._ljm.eStreamRead(self._h())
            n_ch = len(self._scan_names)
            data = np.asarray(raw, dtype=np.float64).reshape(-1, n_ch)
            return data, int(device_backlog)

    def stream_stop(self) -> None:
        with self._io, contextlib.suppress(Exception):
            self._ljm.eStreamStop(self._h())
