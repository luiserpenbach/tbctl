"""Runtime device management: sim <-> USB <-> Ethernet switching.

`SwitchableDevice` is the single DeviceInterface object every service holds
for the life of the process; it delegates to the current backend, so a
switch never leaves stale references in the sequencer/recorder/manual
services. `DeviceManager.switch()` performs the swap under the caller's
guard (sequencer IDLE, not recording, control off — enforced at the API
layer) and never leaves the node half-switched: if the new backend fails
to open, the old one is restored and the stream restarted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Literal

import numpy as np

from tbctl.config.models import ChannelMap
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.events import EventLog

log = logging.getLogger(__name__)

Mode = Literal["sim", "usb", "ethernet"]


class SwitchableDevice:
    """DeviceInterface that delegates every call to a swappable backend."""

    def __init__(self, backend):
        self.backend = backend

    # -- delegation --------------------------------------------------------
    def open(self) -> None:
        self.backend.open()

    def close(self) -> None:
        self.backend.close()

    @property
    def connected(self) -> bool:
        return self.backend.connected

    def read_name(self, name: str) -> float:
        return self.backend.read_name(name)

    def read_names(self, names: list[str]) -> list[float]:
        return self.backend.read_names(names)

    def write_name(self, name: str, value: float) -> None:
        self.backend.write_name(name, value)

    def write_names(self, names: list[str], values: list[float]) -> None:
        self.backend.write_names(names, values)

    def write_name_array(self, name: str, values: list[int]) -> None:
        self.backend.write_name_array(name, values)

    def write_name_bytes(self, name: str, data: bytes) -> None:
        self.backend.write_name_bytes(name, data)

    def read_name_bytes(self, name: str, num_bytes: int) -> bytes:
        return self.backend.read_name_bytes(name, num_bytes)

    def reboot(self, delay_ticks: int = 40) -> None:
        self.backend.reboot(delay_ticks)

    def stream_start(
        self, scan_names: list[str], scan_rate_hz: float, scans_per_read: int
    ) -> float:
        return self.backend.stream_start(scan_names, scan_rate_hz, scans_per_read)

    def stream_read(self) -> tuple[np.ndarray, int]:
        return self.backend.stream_read()

    def stream_stop(self) -> None:
        self.backend.stream_stop()

    # -- sim passthrough ---------------------------------------------------
    @property
    def is_sim(self) -> bool:
        from tbctl.hw.ljm_mock import MockDevice

        return isinstance(self.backend, MockDevice)

    def sim_set(self, tag: str, value_eu: float, noise_sigma_eu: float | None = None) -> None:
        if not self.is_sim:
            raise RuntimeError("not in simulator mode")
        self.backend.sim_set(tag, value_eu, noise_sigma_eu)

    def sim_setpoints(self) -> dict[str, float]:
        if not self.is_sim:
            raise RuntimeError("not in simulator mode")
        return self.backend.sim_setpoints()

    def dio_word16(self) -> int:
        # convenience used by tests; mock-only
        return self.backend.dio_word16()


class DeviceManager:
    def __init__(
        self,
        chmap: ChannelMap,
        device: SwitchableDevice,
        acq: AcquisitionService,
        events: EventLog,
        mode: Mode,
    ):
        self._chmap = chmap
        self.device = device
        self._acq = acq
        self._events = events
        self.mode: Mode = mode
        self.ip: str | None = chmap.device.ip if mode == "ethernet" else None
        self.last_error: str | None = None
        self.switched_at: float = time.time()

    def update_chmap(self, chmap: ChannelMap) -> None:
        """Adopt an edited channel map (used by the config editor)."""
        self._chmap = chmap

    def rebuild_sim_backend(self) -> None:
        """Replace a mock backend so it reflects a new channel map.

        Only meaningful in sim mode; caller must have stopped acquisition.
        """
        if not self.device.is_sim:
            return
        from tbctl.hw.ljm_mock import MockDevice

        old = self.device.backend
        new = MockDevice(self._chmap)
        new.open()
        self.device.backend = new
        with contextlib.suppress(Exception):
            old.close()

    def _build_backend(self, mode: Mode, ip: str | None):
        if mode == "sim":
            from tbctl.hw.ljm_mock import MockDevice

            return MockDevice(self._chmap)
        from tbctl.hw.ljm_iface import LjmDevice

        if mode == "usb":
            return LjmDevice("ANY", "USB")
        return LjmDevice(ip or self._chmap.device.ip, "ETHERNET")

    async def switch(self, mode: Mode, ip: str | None = None) -> None:
        """Swap the backend. Caller must hold the operational guards.

        On failure the previous backend is restored and the stream is
        restarted before the error propagates.
        """
        old_backend = self.device.backend
        old_mode, old_ip = self.mode, self.ip
        log.info("switching device %s -> %s", old_mode, mode)
        await self._acq.stop()
        new_backend = self._build_backend(mode, ip)
        try:
            await asyncio.to_thread(new_backend.open)
            self.device.backend = new_backend
            await self._acq.start()
        except Exception as e:
            self.last_error = f"switch to {mode} failed: {e}"
            log.error("%s — restoring %s", self.last_error, old_mode)
            with contextlib.suppress(Exception):
                new_backend.close()
            self.device.backend = old_backend
            if not old_backend.connected:
                await asyncio.to_thread(old_backend.open)
            await self._acq.start()
            self.mode, self.ip = old_mode, old_ip
            raise
        self.mode, self.ip = mode, ip if mode == "ethernet" else None
        self.last_error = None
        self.switched_at = time.time()
        with contextlib.suppress(Exception):
            old_backend.close()
        self._events.log("connection_switched", mode=mode, ip=self.ip)

    def info(self) -> dict:
        """Connection detail for GET /api/connection (best-effort reads)."""
        out: dict = {
            "mode": self.mode,
            "ip": self.ip or self._chmap.device.ip,
            "connected": self.device.connected,
            "sim": self.device.is_sim,
            "last_error": self.last_error,
            "switched_at": self.switched_at,
            "stream": {
                "running": self._acq.running,
                "scan_rate_hz": self._acq.scan_rate_hz,
                "sample_count": self._acq.sample_count,
            },
        }
        try:
            vals = self.device.read_names(
                ["SERIAL_NUMBER", "FIRMWARE_VERSION", "TEMPERATURE_DEVICE_K"]
            )
            out["device"] = {
                "serial": int(vals[0]),
                "firmware": round(float(vals[1]), 4),
                "temp_degc": round(float(vals[2]) - 273.15, 1),
            }
        except Exception as e:
            out["device"] = {"error": str(e)}
        try:
            from tbctl.hw.registers_gen import INTERP_VERSION, REG_NAME

            v = int(self.device.read_name(REG_NAME["INTERP_VERSION"]))
            out["interpreter"] = {
                "version": v,
                "expected": INTERP_VERSION,
                "ok": v == INTERP_VERSION,
            }
        except Exception as e:
            out["interpreter"] = {"error": str(e)}
        try:
            out["watchdog_enabled"] = bool(self.device.read_name("WATCHDOG_ENABLE_DEFAULT"))
        except Exception:
            out["watchdog_enabled"] = None
        return out
