"""Append-only event log (spec §7): per-day JSONL + fan-out to sinks.

Every operational event (run start/stop, arm/fire/abort, faults, manual
actuation, config reloads) goes through here. Sinks are best-effort
callables — the recorder mirrors events into the active run's HDF5, the WS
hub broadcasts them to clients.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

EventSink = Callable[[dict], None]


class EventLog:
    def __init__(self, log_dir: Path):
        self._dir = log_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._sinks: list[EventSink] = []

    def add_sink(self, sink: EventSink) -> Callable[[], None]:
        self._sinks.append(sink)
        return lambda: self._sinks.remove(sink)

    def log(self, event: str, **payload) -> dict:
        record = {
            "time": time.time(),
            "iso": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event": event,
            **payload,
        }
        path = self._dir / f"events-{datetime.now(UTC):%Y%m%d}.jsonl"
        try:
            with path.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            log.exception("failed to append event log")
        for sink in list(self._sinks):
            try:
                sink(record)
            except Exception:
                log.exception("event sink failed")
        return record
