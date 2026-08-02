"""Manual single-valve actuation (spec §3): only when the sequencer is IDLE
and control mode is enabled. Every actuation is an event."""

from __future__ import annotations

import logging
import threading

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
        # per-spark auto-off timers (coil protection, D9) — the Lua max_on_ms
        # guard only runs inside a sequence, so manual spark-on needs its own
        self._spark_timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def _cancel_spark_timer(self, tag: str) -> None:
        t = self._spark_timers.pop(tag, None)
        if t is not None:
            t.cancel()

    def _spark_auto_off(self, ch: SparkChannel) -> None:
        """Fired by the timer: force the igniter to its safe level."""
        safe = 1 - self._chmap.active_level(ch)
        with self._lock:
            self._spark_timers.pop(ch.tag, None)
            try:
                self._iface.write_name(ch.hw, float(safe))
                self._events.log(
                    "manual_spark_auto_off", tag=ch.tag, hw=ch.hw, max_on_ms=ch.max_on_ms
                )
                log.warning("manual spark %s auto-off after %d ms", ch.tag, ch.max_on_ms)
            except Exception:
                log.exception("manual spark auto-off write failed for %s", ch.tag)

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
        with self._lock:
            self._iface.write_name(ch.hw, float(level))
            if isinstance(ch, SparkChannel):
                # (re)start or clear the coil-protection auto-off
                self._cancel_spark_timer(tag)
                if active:
                    timer = threading.Timer(ch.max_on_ms / 1000.0, self._spark_auto_off, args=(ch,))
                    timer.daemon = True
                    self._spark_timers[tag] = timer
                    timer.start()
            self._events.log("manual_actuation", tag=tag, state=state, hw=ch.hw, level=level)
        return {"tag": tag, "state": state, "hw": ch.hw}

    def shutdown(self) -> None:
        with self._lock:
            for t in self._spark_timers.values():
                t.cancel()
            self._spark_timers.clear()
