"""Upload lua/sequencer.lua to the T7 and start it (LUA_* registers).

Deployment flow (LabJack T-series Lua scripting registers):
  1. LUA_RUN = 0, wait for the interpreter to stop
  2. LUA_SOURCE_SIZE = len(source) + 1 (NUL terminator)
  3. LUA_SOURCE_WRITE <- source bytes
  4. LUA_RUN = 1
  5. optionally LUA_SAVE_TO_FLASH + LUA_RUN_DEFAULT=1 so the script starts
     at power-on (required for the power-cycle-while-ARMED acceptance test)

After start, the script publishes INTERP_VERSION; refuse to proceed on
mismatch (§6.1).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.hw.registers_gen import INTERP_VERSION, REG_NAME

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEQUENCER_LUA = REPO_ROOT / "lua" / "sequencer.lua"


class ScriptLoadError(Exception):
    pass


def stop_script(iface: DeviceInterface, timeout_s: float = 3.0) -> None:
    iface.write_name("LUA_RUN", 0)
    deadline = time.monotonic() + timeout_s
    while iface.read_name("LUA_RUN") != 0:
        if time.monotonic() > deadline:
            raise ScriptLoadError("Lua interpreter did not stop")
        time.sleep(0.05)


def deploy_script(
    iface: DeviceInterface,
    source: str | None = None,
    save_to_flash: bool = True,
    timeout_s: float = 5.0,
) -> int:
    """Upload + start the sequencer script; returns the reported interp version."""
    if source is None:
        source = SEQUENCER_LUA.read_text()
    payload = source.encode("ascii") + b"\x00"

    stop_script(iface)
    iface.write_name("LUA_SOURCE_SIZE", len(payload))
    iface.write_name_array("LUA_SOURCE_WRITE", list(payload))
    if save_to_flash:
        iface.write_name("LUA_SAVE_TO_FLASH", 1)
        iface.write_name("LUA_RUN_DEFAULT", 1)  # run at power-on
    iface.write_name("LUA_RUN", 1)

    # wait for the script to boot and publish its version
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        v = int(iface.read_name(REG_NAME["INTERP_VERSION"]))
        if v:
            if v != INTERP_VERSION:
                raise ScriptLoadError(
                    f"deployed script reports interp_version {v}, host expects {INTERP_VERSION}"
                )
            log.info("sequencer.lua running, interp_version %d", v)
            return v
        time.sleep(0.1)
    raise ScriptLoadError("script did not publish INTERP_VERSION (crashed? check LUA_DEBUG)")


def read_debug(iface: DeviceInterface, max_bytes: int = 1024) -> str:
    """Fetch the Lua debug buffer (useful when the script won't start)."""
    n = int(iface.read_name("LUA_DEBUG_NUM_BYTES"))
    n = min(n, max_bytes)
    if n <= 0:
        return ""
    chars = [int(iface.read_name("LUA_DEBUG_DATA")) for _ in range(n)]
    return "".join(chr(c) for c in chars if 0 < c < 128)
