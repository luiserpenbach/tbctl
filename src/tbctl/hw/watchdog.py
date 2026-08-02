"""T7 hardware watchdog configuration (spec §6, safety layer 2).

If ALL communication with the T7 stops (node dead, Lua dead, cable pulled
after the Lua also died), the hardware watchdog forces the controlled DIO
lines to their fail-safe image after `watchdog_timeout_s`. This is the
backstop below the Lua heartbeat abort; the hardwired E-stop sits below
both, outside software.

Verified on the bench by killing the Lua script while ARMED (§10.3).

WATCHDOG_* register names are resolved by LJM from ljm_constants.json; the
fail-safe image comes from the channel map (never hardcoded).
"""

from __future__ import annotations

import logging

from tbctl.config.models import ChannelMap
from tbctl.hw.ljm_iface import DeviceInterface

log = logging.getLogger(__name__)

# Grace period before the watchdog starts enforcing after (re)enable/boot.
# Keep this SHORT: §10.3 test 4 showed the watchdog never fired within the
# test window with a 10 s delay (the T7 was still in grace when comm resumed,
# which resets the timer). 3 s is ample for the node to begin polling.
STARTUP_DELAY_S = 3.0


def configure_watchdog(iface: DeviceInterface, chmap: ChannelMap) -> dict:
    """Program the watchdog defaults; returns the applied settings."""
    safe_word = chmap.safe_word()
    controlled = chmap.controlled_mask()
    inhibit32 = 0xFFFF0000 | chmap.dio_inhibit()
    timeout_s = chmap.device.watchdog_timeout_s

    settings: list[tuple[str, float]] = [
        ("WATCHDOG_ENABLE_DEFAULT", 0),  # disable while (re)configuring
        ("WATCHDOG_TIMEOUT_S_DEFAULT", float(timeout_s)),
        ("WATCHDOG_STARTUP_DELAY_S_DEFAULT", STARTUP_DELAY_S),
        ("WATCHDOG_DIO_ENABLE_DEFAULT", 1),
        ("WATCHDOG_DIO_STATE_DEFAULT", float(safe_word)),
        ("WATCHDOG_DIO_INHIBIT_DEFAULT", float(inhibit32)),
        ("WATCHDOG_DIO_DIRECTION_DEFAULT", float(controlled)),  # outputs stay outputs
        ("WATCHDOG_RESET_ENABLE_DEFAULT", 0),  # set outputs, do not reboot the device
        ("WATCHDOG_ENABLE_DEFAULT", 1),
    ]
    for name, value in settings:
        iface.write_name(name, value)
    applied = {name: value for name, value in settings}
    log.info(
        "watchdog armed: timeout %ds, safe_word=0x%04X, controlled=0x%04X",
        timeout_s,
        safe_word,
        controlled,
    )
    return applied


def disable_watchdog(iface: DeviceInterface) -> None:
    iface.write_name("WATCHDOG_ENABLE_DEFAULT", 0)
    log.info("watchdog disabled")
