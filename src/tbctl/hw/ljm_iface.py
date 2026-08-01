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

    def open(self) -> None:
        from labjack import ljm  # deferred: needs LJM shared library

        self._ljm = ljm
        self._handle = ljm.openS("T7", self._connection, self._identifier)

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._ljm.close(self._handle)
            finally:
                self._handle = None

    @property
    def connected(self) -> bool:
        return self._handle is not None

    def _h(self) -> int:
        if self._handle is None:
            raise DeviceError("device not open")
        return self._handle

    def read_name(self, name: str) -> float:
        return self._ljm.eReadName(self._h(), name)

    def read_names(self, names: list[str]) -> list[float]:
        return list(self._ljm.eReadNames(self._h(), len(names), names))

    def write_name(self, name: str, value: float) -> None:
        self._ljm.eWriteName(self._h(), name, value)

    def write_names(self, names: list[str], values: list[float]) -> None:
        self._ljm.eWriteNames(self._h(), len(names), names, values)

    def write_name_array(self, name: str, values: list[int]) -> None:
        self._ljm.eWriteNameArray(self._h(), name, len(values), values)

    def stream_start(
        self, scan_names: list[str], scan_rate_hz: float, scans_per_read: int
    ) -> float:
        addrs = self._ljm.namesToAddresses(len(scan_names), scan_names)[0]
        self._scan_names = list(scan_names)
        self._scans_per_read = scans_per_read
        actual = self._ljm.eStreamStart(self._h(), scans_per_read, len(addrs), addrs, scan_rate_hz)
        return float(actual)

    def stream_read(self) -> tuple[np.ndarray, int]:
        raw, device_backlog, _ljm_backlog = self._ljm.eStreamRead(self._h())
        n_ch = len(self._scan_names)
        data = np.asarray(raw, dtype=np.float64).reshape(-1, n_ch)
        return data, int(device_backlog)

    def stream_stop(self) -> None:
        with contextlib.suppress(Exception):
            self._ljm.eStreamStop(self._h())
