"""Upload lua/sequencer.lua to the T7 and start it (LUA_* registers).

Deployment flow (LabJack T-series Lua scripting registers):
  1. Release shared RAM (FIFO/watchdog/AIN_EF) + stop Lua
  2. LUA_SOURCE_SIZE = len(source) + 1 (NUL terminator)
  3. LUA_SOURCE_WRITE <- source bytes (ByteArray API)
  4. LUA_RUN = 1, wait for INTERP_VERSION
  5. optionally LUA_SAVE_TO_FLASH + LUA_RUN_DEFAULT=1 *after* a successful start

After start, the script publishes INTERP_VERSION; refuse to proceed on
mismatch (sec 6.1).

T7 Lua RAM is tight (64KB shared heap, ~4 KiB practical script budget).
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path

from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.hw.registers_gen import INTERP_VERSION, REG_NAME

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
SEQUENCER_LUA = REPO_ROOT / "lua" / "sequencer.lua"

_PURGE_LUA = (
    b"MB.W(46189,0,0)\n"
    b"LJ.IntervalConfig(0,100)\n"
    b"while true do if LJ.CheckInterval(0) then end end\n\x00"
)


class ScriptLoadError(Exception):
    pass


def minify_lua(source: str) -> str:
    """Strip comments and collapse whitespace for T7 RAM limits."""
    kept: list[str] = []
    for raw in source.splitlines():
        line = raw.split("--", 1)[0].strip()
        if line:
            kept.append(line)
    text = " ".join(kept)
    text = re.sub(r"\s+", " ", text).strip()
    # Tighten around punctuation; keep spaces that Lua keywords need.
    text = re.sub(r"\s*([=+\-*/%<>~^,]|==|~=|<=|>=)\s*", r"\1", text)
    text = re.sub(r"\s*([(){}\[\];])\s*", r"\1", text)
    for kw in (
        "local",
        "function",
        "return",
        "if",
        "then",
        "else",
        "elseif",
        "end",
        "for",
        "do",
        "while",
        "not",
        "and",
        "or",
        "in",
    ):
        text = re.sub(rf"\b{kw}\b", f" {kw} ", text)
    return re.sub(r"\s+", " ", text).strip() + "\n"


def stop_script(iface: DeviceInterface, timeout_s: float = 3.0) -> None:
    iface.write_name("LUA_RUN", 0)
    time.sleep(0.6)
    iface.write_name("LUA_RUN", 0)
    deadline = time.monotonic() + timeout_s
    while iface.read_name("LUA_RUN") != 0:
        if time.monotonic() > deadline:
            raise ScriptLoadError("Lua interpreter did not stop")
        time.sleep(0.05)
    time.sleep(0.2)


def _upload_and_run(
    iface: DeviceInterface, payload: bytes, *, debug: bool = False
) -> None:
    iface.write_name("LUA_SOURCE_SIZE", len(payload))
    iface.write_name_bytes("LUA_SOURCE_WRITE", payload)
    iface.write_name("LUA_DEBUG_ENABLE", 1 if debug else 0)
    iface.write_name("LUA_RUN", 1)


def release_shared_ram(iface: DeviceInterface) -> None:
    """Free T7 shared-RAM consumers that starve Lua compiles."""
    stop_script(iface)
    with contextlib.suppress(Exception):
        iface.write_name("LUA_RUN_DEFAULT", 0)
    with contextlib.suppress(Exception):
        iface.write_name("WATCHDOG_ENABLE_DEFAULT", 0)
    for i in range(4):
        with contextlib.suppress(Exception):
            iface.write_name(f"USER_RAM_FIFO{i}_ALLOCATE_NUM_BYTES", 0)
    with contextlib.suppress(Exception):
        iface.stream_stop()
    for i in range(14):
        with contextlib.suppress(Exception):
            iface.write_name(f"AIN{i}_EF_INDEX", 0)


def purge_lua_ram(iface: DeviceInterface) -> None:
    """Force a clean Lua VM so the next full upload is not fragmented/OOM."""
    release_shared_ram(iface)
    _upload_and_run(iface, _PURGE_LUA, debug=False)
    time.sleep(0.2)
    stop_script(iface)
    with contextlib.suppress(Exception):
        iface.write_name(REG_NAME["FIFO_ALLOCATE"], 0)


def deploy_script(
    iface: DeviceInterface,
    source: str | None = None,
    save_to_flash: bool = True,
    timeout_s: float = 5.0,
    minify: bool = True,
) -> int:
    """Upload + start the sequencer script; returns the reported interp version."""
    if source is None:
        source = SEQUENCER_LUA.read_text(encoding="utf-8")
    if minify:
        source = minify_lua(source)
        log.info("minified sequencer.lua to %d bytes", len(source))
    payload = source.encode("ascii") + b"\x00"

    def _wait_for_version() -> int | None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            v = int(iface.read_name(REG_NAME["INTERP_VERSION"]))
            if v:
                if v != INTERP_VERSION:
                    raise ScriptLoadError(
                        f"deployed script reports interp_version {v}, host expects {INTERP_VERSION}"
                    )
                return v
            time.sleep(0.1)
        return None

    def _finish(v: int) -> int:
        if save_to_flash:
            # Internal flash is briefly busy after SYSTEM_REBOOT.
            last_err: Exception | None = None
            for _ in range(10):
                try:
                    iface.write_name("LUA_SAVE_TO_FLASH", 1)
                    iface.write_name("LUA_RUN_DEFAULT", 1)
                    last_err = None
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    time.sleep(0.5)
            if last_err is not None:
                log.warning("script running but flash save failed: %s", last_err)
        log.info("sequencer.lua running, interp_version %d", v)
        return v

    def _attempt(*, debug: bool) -> int | None:
        release_shared_ram(iface)
        iface.write_name(REG_NAME["INTERP_VERSION"], 0)
        _upload_and_run(iface, payload, debug=debug)
        return _wait_for_version()

    def _reboot_and_retry(*, debug: bool) -> int | None:
        if not hasattr(iface, "reboot"):
            return _attempt(debug=debug)
        log.warning("soft-rebooting T7 to reclaim shared heap / recover USB")
        iface.reboot()
        time.sleep(2.0)  # let internal flash settle after reboot
        return _attempt(debug=debug)

    try:
        v = _attempt(debug=False)
    except Exception as e:  # noqa: BLE001 — USB drops mid-deploy; reboot recovers
        log.warning("deploy attempt failed (%s); rebooting", e)
        v = _reboot_and_retry(debug=True)
    else:
        if v is None:
            v = _reboot_and_retry(debug=True)

    if v is not None:
        return _finish(v)

    raise ScriptLoadError("script did not publish INTERP_VERSION (crashed? check LUA_DEBUG)")


def read_debug(iface: DeviceInterface, max_bytes: int = 1024) -> str:
    """Fetch the Lua debug buffer (useful when the script won't start)."""
    n = int(iface.read_name("LUA_DEBUG_NUM_BYTES"))
    n = min(n, max_bytes)
    if n <= 0:
        return ""
    raw = iface.read_name_bytes("LUA_DEBUG_DATA", n)
    return raw.decode("ascii", errors="replace")
