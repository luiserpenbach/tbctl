"""Upload lua/sequencer.lua to the T7 and start it (LUA_* registers).

Deployment flow (LabJack T-series Lua scripting registers):
  1. Release shared RAM (FIFO/watchdog/AIN_EF) + stop Lua (LUA_RUN_DEFAULT=0)
  2. LUA_SOURCE_SIZE = len(source) + 1 (NUL terminator)
  3. LUA_SOURCE_WRITE <- source bytes (ByteArray API)
  4. optionally LUA_SAVE_TO_FLASH, then LUA_LOAD_SAVED + LUA_SOURCE_SIZE readback
  5. LUA_RUN = 1, wait for INTERP_VERSION
  6. optionally LUA_RUN_DEFAULT=1 *after* a successful start

LUA_SAVE_TO_FLASH saves the *loaded source buffer*, and LUA_RUN consumes that
buffer when it compiles. Saving after LUA_RUN (as this module did until
2026-10-22) wrote an empty script and still reported success, so nothing ran
after a power cycle (TODO.md sec 3, test 5). Save with the script stopped and
the source freshly written (Kipling's order), then run the copy loaded back
from flash, so a successful start proves the saved script.

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


# LUA_SOURCE_SIZE after LUA_LOAD_SAVED may exceed the uploaded size by flash
# padding (+8 bytes seen on fw 1.0332).
_FLASH_PAD_MAX = 16


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


def _upload(iface: DeviceInterface, payload: bytes) -> None:
    iface.write_name("LUA_SOURCE_SIZE", len(payload))
    iface.write_name_bytes("LUA_SOURCE_WRITE", payload)


def _run(iface: DeviceInterface, *, debug: bool = False) -> None:
    iface.write_name("LUA_DEBUG_ENABLE", 1 if debug else 0)
    iface.write_name("LUA_RUN", 1)


def _upload_and_run(iface: DeviceInterface, payload: bytes, *, debug: bool = False) -> None:
    _upload(iface, payload)
    _run(iface, debug=debug)


def _write_retrying(iface: DeviceInterface, name: str, value: float, tries: int = 10) -> None:
    # Internal flash is briefly busy after SYSTEM_REBOOT and after a save.
    for i in range(tries):
        try:
            iface.write_name(name, value)
            return
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(0.5)


def save_loaded_to_flash(iface: DeviceInterface, size: int) -> None:
    """Save the stopped, just-uploaded source to flash and load it back.

    Leaves the flash copy in the source buffer, ready for LUA_RUN. Raises if
    the copy read back from flash is shorter than `size` bytes (the empty
    save this guards against) or implausibly longer. fw 1.0332 reads back
    size + 8 (padding); running the flash copy afterwards is the real proof.
    """
    try:
        _write_retrying(iface, "LUA_SAVE_TO_FLASH", 1)
        _write_retrying(iface, "LUA_LOAD_SAVED", 1)
        saved = int(iface.read_name("LUA_SOURCE_SIZE"))
    except Exception as e:
        raise ScriptLoadError(f"saving sequencer.lua to flash failed ({e})") from e
    if not size <= saved <= size + _FLASH_PAD_MAX:
        raise ScriptLoadError(
            f"flash holds a {saved}-byte script after LUA_SAVE_TO_FLASH, expected {size}"
        )


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
            try:
                _write_retrying(iface, "LUA_RUN_DEFAULT", 1)
            except Exception as e:
                # the saved script is fine, but the T7 will not start it at boot
                raise ScriptLoadError(
                    f"script saved and running but LUA_RUN_DEFAULT=1 failed ({e}); it "
                    "will not autostart after a power cycle — redeploy"
                ) from e
            log.info("sequencer.lua saved to flash (%d bytes), autostart enabled", len(payload))
        log.info("sequencer.lua running, interp_version %d", v)
        return v

    def _attempt(*, debug: bool) -> int | None:
        release_shared_ram(iface)
        iface.write_name(REG_NAME["INTERP_VERSION"], 0)
        _upload(iface, payload)
        if save_to_flash:
            save_loaded_to_flash(iface, len(payload))
        _run(iface, debug=debug)
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
    except Exception as e:
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
