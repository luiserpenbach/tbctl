"""Redline watchers: host-side auto-abort on sensor limits.

Armed together with the sequence and disarmed on any terminal state. The
watcher taps the acquisition listener (every ingested block, engineering
units), tracks per-redline violation persistence, and on a trip fires the
sequencer's ABORT — the same always-accepted fastest path as the operator
button — plus an event.

This is deliberately a monitoring layer ABOVE the Lua safety chain: the T7
still enforces heartbeat, spark guard and the abort table autonomously.
Redline reaction time is bounded by the 50 Hz sample rate plus one stream
block (~100 ms worst case), which is the right tool for tank overpressure
or wall temperature — not a replacement for the 1 ms Lua guards.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

import numpy as np

from tbctl.config.models import Redline
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.events import EventLog

log = logging.getLogger(__name__)


class RedlineWatcher:
    def __init__(
        self,
        acq: AcquisitionService,
        events: EventLog,
        abort: Callable[[str], Awaitable[None]],
    ):
        self._acq = acq
        self._events = events
        self._abort = abort
        self._redlines: list[Redline] = []
        self._first_violation: dict[int, float] = {}  # redline idx -> epoch of streak start
        self._tripped = False
        self._unsubscribe: Callable[[], None] | None = None
        self._tag_cols: dict[str, tuple[str, int]] = {}  # tag -> (group, column)
        self._abort_task: asyncio.Task | None = None  # keep the trip task alive

    @property
    def armed(self) -> bool:
        return self._unsubscribe is not None

    @property
    def active_redlines(self) -> list[Redline]:
        return list(self._redlines)

    def arm(self, redlines: list[Redline]) -> None:
        """Start watching. Called by the sequencer at ARM."""
        self.disarm()
        if not redlines:
            return
        self._redlines = list(redlines)
        self._first_violation = {}
        self._tripped = False
        self._tag_cols = {}
        for ring_name, ring in self._acq.rings.items():
            for col, tag in enumerate(ring.tags):
                self._tag_cols[tag] = (ring_name, col)
        self._unsubscribe = self._acq.add_listener(self._on_block)
        log.info("redlines armed: %s", [r.tag for r in redlines])

    def disarm(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
            self._redlines = []
            self._first_violation = {}
            log.info("redlines disarmed")

    # -- stream path (event-loop thread, must stay cheap) -------------------

    def _on_block(self, group: str, t: np.ndarray, block: np.ndarray) -> None:
        if self._tripped:
            return
        for idx, rl in enumerate(self._redlines):
            loc = self._tag_cols.get(rl.tag)
            if loc is None or loc[0] != group:
                continue
            values = block[:, loc[1]]
            for k in range(len(values)):
                v = float(values[k])
                if not math.isfinite(v):
                    # A redlined sensor producing NaN/inf (dead channel, failed
                    # derived expression, LJM dummy sample) cannot be shown to
                    # be within limits — treat it as a violation and keep the
                    # persistence streak, never as an in-bounds sample that
                    # silently resets the streak.
                    violated = True
                else:
                    violated = (rl.min is not None and v < rl.min) or (
                        rl.max is not None and v > rl.max
                    )
                if not violated:
                    self._first_violation.pop(idx, None)
                    continue
                start = self._first_violation.setdefault(idx, float(t[k]))
                if (float(t[k]) - start) * 1000.0 >= rl.persist_ms:
                    self._trip(rl, v)
                    return

    def _trip(self, rl: Redline, value: float) -> None:
        self._tripped = True
        if not math.isfinite(value):
            limit = "non-finite"
        elif rl.min is not None and value < rl.min:
            limit = f"min {rl.min}"
        else:
            limit = f"max {rl.max}"
        reason = f"redline {rl.tag} {limit}"
        log.error("REDLINE %s = %.3f (%s) — aborting", rl.tag, value, limit)
        # Command the abort FIRST, before any blocking work: the event log does
        # a synchronous JSONL append + recorder HDF5 fan-out which can stall the
        # loop for hundreds of ms on a busy SD card — that latency must not be
        # added ahead of the tank-overpressure abort.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # listener always runs in the loop thread; fallback for direct tests
            asyncio.run(self._abort(reason))
        else:
            self._abort_task = loop.create_task(self._abort(reason))
        self._events.log(
            "redline_tripped",
            tag=rl.tag,
            value=round(value, 3),
            limit=limit,
            persist_ms=rl.persist_ms,
        )

    # -- introspection ------------------------------------------------------

    def status(self) -> dict:
        return {
            "armed": self.armed,
            "tripped": self._tripped,
            "redlines": [
                {"tag": r.tag, "min": r.min, "max": r.max, "persist_ms": r.persist_ms}
                for r in self._redlines
            ],
            "time": time.time(),
        }
