"""Manual single-valve actuation (spec §3): only when the sequencer is IDLE
and control mode is enabled. Every actuation is an event."""

from __future__ import annotations

import logging

from tbctl.config.models import ChannelMap, SparkChannel, ValveChannel
from tbctl.hw.ljm_iface import DeviceInterface
from tbctl.hw.registers_gen import REG_NAME, SeqState
from tbctl.services.events import EventLog

log = logging.getLogger(__name__)


class ManualControlError(Exception):
    pass


class ManualControl:
    def __init__(self, chmap: ChannelMap, iface: DeviceInterface, events: EventLog):
        self._chmap = chmap
        self._iface = iface
        self._events = events

    def set_output(self, tag: str, state: str, control_enabled: bool) -> dict:
        if not control_enabled:
            raise ManualControlError("control mode is disabled")
        seq_state = SeqState(int(self._iface.read_name(REG_NAME["STATE"])))
        if seq_state != SeqState.IDLE:
            raise ManualControlError(f"sequencer is {seq_state.name}; manual control locked out")
        try:
            ch = self._chmap.by_tag(tag)
        except KeyError as e:
            raise ManualControlError(str(e)) from e
        if isinstance(ch, ValveChannel):
            if state not in ("open", "close"):
                raise ManualControlError(f"valve takes open/close, got {state!r}")
            active = state == "open"
        elif isinstance(ch, SparkChannel):
            if state not in ("on", "off"):
                raise ManualControlError(f"spark takes on/off, got {state!r}")
            active = state == "on"
        else:
            raise ManualControlError(f"{tag!r} is not an output channel")
        level = self._chmap.active_level(ch) if active else 1 - self._chmap.active_level(ch)
        self._iface.write_name(ch.hw, float(level))
        self._events.log("manual_actuation", tag=tag, state=state, hw=ch.hw, level=level)
        return {"tag": tag, "state": state, "hw": ch.hw}
