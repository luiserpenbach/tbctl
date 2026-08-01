"""Heartbeat publisher (spec §3 services/safety.py).

While a sequence is armed or running, the host increments the HEARTBEAT
USER_RAM register at 20 Hz (spec requires >=10 Hz). If the node dies, hangs
or the cable is pulled, the Lua side sees a stale heartbeat within
HB_TIMEOUT_MS and runs the abort table autonomously — that is safety layer
1 (layer 2 is the T7 hardware watchdog, layer 3 the hardwired E-stop).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.hw.registers_gen import REG_NAME

log = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_S = 0.05  # 20 Hz


class HeartbeatPublisher:
    def __init__(self, iface: DeviceInterface):
        self._iface = iface
        self._task: asyncio.Task | None = None
        self._counter = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="heartbeat")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        name = REG_NAME["HEARTBEAT"]
        while True:
            self._counter = (self._counter + 1) & 0xFFFF
            try:
                await asyncio.to_thread(self._iface.write_name, name, float(self._counter))
            except Exception:
                log.exception("heartbeat write failed")
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
