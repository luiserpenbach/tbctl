"""Run lifecycle + HDF5 writer (spec §3, D11, D12).

Per-run HDF5 on disk is the source of truth. Layout:

    /                       attrs: schema_version, run metadata, config SHAs,
                            interp_version, tbctl git hash, UTC timestamps,
                            per-group sample counts (accounting for §10.4)
    /groups/<rate_group>/t     (N,)     f8   epoch seconds
    /groups/<rate_group>/data  (N, nch) f8   engineering units
                            attrs: tags, rate_hz
    /events                 vlen str, JSON records mirrored from the event log

The recorder taps AcquisitionService's listener hook, buffers in memory and
flushes on a short timer, so the stream path never blocks on disk I/O.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import h5py
import numpy as np

from tbctl.config.models import ChannelMap
from tbctl.hw.registers_gen import INTERP_VERSION
from tbctl.services.acquisition import AcquisitionService, envelope_decimate

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FLUSH_INTERVAL_S = 0.5


class RecorderError(Exception):
    pass


@dataclass
class RunInfo:
    run_id: str
    path: Path
    name: str
    operator: str
    notes: str
    t_start: float
    t_stop: float | None = None
    sample_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "path": str(self.path),
            "name": self.name,
            "operator": self.operator,
            "notes": self.notes,
            "t_start": self.t_start,
            "t_stop": self.t_stop,
            "sample_counts": self.sample_counts,
        }


class Recorder:
    def __init__(
        self,
        chmap: ChannelMap,
        channels_sha256: str,
        acq: AcquisitionService,
        data_dir: Path,
        git_hash: str = "unknown",
    ):
        self._chmap = chmap
        self._channels_sha = channels_sha256
        self._acq = acq
        self._data_dir = data_dir
        self._git_hash = git_hash
        self._file: h5py.File | None = None
        self._run: RunInfo | None = None
        self._pending: dict[str, list[tuple[np.ndarray, np.ndarray]]] = {}
        self._pending_events: list[str] = []
        self._unsubscribe = None
        self._flush_task: asyncio.Task | None = None
        self.sequence_sha256: str = ""  # set by the sequencer when a table is loaded

    @property
    def active(self) -> RunInfo | None:
        return self._run

    # -- lifecycle ---------------------------------------------------------

    async def start_run(self, name: str, operator: str, notes: str = "") -> RunInfo:
        if self._run is not None:
            raise RecorderError(f"run {self._run.run_id} already recording")
        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", name).strip("_") or "run"
        t0 = time.time()
        run_id = f"{datetime.fromtimestamp(t0, UTC):%Y%m%d-%H%M%S}-{safe}"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / f"{run_id}.h5"
        f = await asyncio.to_thread(h5py.File, path, "x")
        f.attrs.update(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": run_id,
                "name": name,
                "operator": operator,
                "notes": notes,
                "channels_sha256": self._channels_sha,
                "sequence_sha256": self.sequence_sha256,
                "interp_version": INTERP_VERSION,
                "tbctl_git_hash": self._git_hash,
                "t_start": t0,
                "t_start_utc": datetime.fromtimestamp(t0, UTC).isoformat(),
            }
        )
        grp = f.create_group("groups")
        for group, ring in self._acq.rings.items():
            g = grp.create_group(group)
            g.attrs["tags"] = ring.tags
            g.attrs["rate_hz"] = ring.rate_hz
            g.create_dataset("t", shape=(0,), maxshape=(None,), dtype="f8", chunks=(4096,))
            g.create_dataset(
                "data",
                shape=(0, len(ring.tags)),
                maxshape=(None, len(ring.tags)),
                dtype="f8",
                chunks=(4096, len(ring.tags)),
            )
        f.create_dataset("events", shape=(0,), maxshape=(None,), dtype=h5py.string_dtype())
        self._file = f
        self._run = RunInfo(run_id, path, name, operator, notes, t0)
        self._pending = {g: [] for g in self._acq.rings}
        self._pending_events = []
        self._unsubscribe = self._acq.add_listener(self._on_block)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="recorder-flush")
        log.info("run %s started -> %s", run_id, path)
        return self._run

    async def stop_run(self) -> RunInfo:
        if self._run is None:
            raise RecorderError("no active run")
        run = self._run
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._flush_task:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        await asyncio.to_thread(self._flush)
        run.t_stop = time.time()
        f = self._file
        assert f is not None
        for group in self._acq.rings:
            run.sample_counts[group] = f["groups"][group]["t"].shape[0]
        f.attrs["t_stop"] = run.t_stop
        f.attrs["t_stop_utc"] = datetime.fromtimestamp(run.t_stop, UTC).isoformat()
        f.attrs["sample_counts"] = json.dumps(run.sample_counts)
        await asyncio.to_thread(f.close)
        self._file = None
        self._run = None
        log.info("run %s stopped (%s samples)", run.run_id, run.sample_counts)
        return run

    # -- data & event ingest ----------------------------------------------

    def _on_block(self, group: str, t: np.ndarray, block: np.ndarray) -> None:
        self._pending[group].append((t, block))

    def on_event(self, record: dict) -> None:
        """EventLog sink: mirror events into the active run file."""
        if self._run is not None:
            self._pending_events.append(json.dumps(record))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            await asyncio.to_thread(self._flush)

    def _flush(self) -> None:
        f = self._file
        if f is None:
            return
        for group, blocks in self._pending.items():
            if not blocks:
                continue
            self._pending[group] = []
            t = np.concatenate([b[0] for b in blocks])
            v = np.concatenate([b[1] for b in blocks])
            g = f["groups"][group]
            n0 = g["t"].shape[0]
            g["t"].resize((n0 + len(t),))
            g["t"][n0:] = t
            g["data"].resize((n0 + len(t), v.shape[1]))
            g["data"][n0:] = v
        if self._pending_events:
            evs, self._pending_events = self._pending_events, []
            d = f["events"]
            n0 = d.shape[0]
            d.resize((n0 + len(evs),))
            d[n0:] = evs
        f.flush()

    # -- historical access -------------------------------------------------

    def list_runs(self) -> list[dict]:
        out = []
        for path in sorted(self._data_dir.glob("*.h5"), reverse=True):
            if self._run and path == self._run.path:
                continue  # active run listed via status
            try:
                with h5py.File(path, "r") as f:
                    out.append(_meta_from(f, path))
            except OSError:
                log.warning("unreadable run file %s", path)
        return out

    def run_meta(self, run_id: str) -> dict:
        path = self._run_path(run_id)
        with h5py.File(path, "r") as f:
            return _meta_from(f, path)

    def _run_path(self, run_id: str) -> Path:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", run_id):
            raise KeyError(f"bad run id {run_id!r}")
        path = self._data_dir / f"{run_id}.h5"
        if not path.is_file():
            raise KeyError(f"unknown run {run_id!r}")
        if self._run and path == self._run.path:
            raise RecorderError("run is still recording")
        return path

    def run_data(
        self,
        run_id: str,
        tags: list[str],
        t0: float | None = None,
        t1: float | None = None,
        max_points: int = 2000,
    ) -> dict[str, dict[str, list[float]]]:
        path = self._run_path(run_id)
        out: dict[str, dict[str, list[float]]] = {}
        with h5py.File(path, "r") as f:
            for tag in tags:
                found = False
                for group in f["groups"]:
                    g_tags = list(f["groups"][group].attrs["tags"])
                    if tag in g_tags:
                        col = g_tags.index(tag)
                        t = f["groups"][group]["t"][:]
                        lo = int(np.searchsorted(t, t0, "left")) if t0 is not None else 0
                        hi = int(np.searchsorted(t, t1, "right")) if t1 is not None else len(t)
                        v = f["groups"][group]["data"][lo:hi, col]
                        out[tag] = envelope_decimate(t[lo:hi], v, max_points)
                        found = True
                        break
                if not found:
                    raise KeyError(f"tag {tag!r} not in run {run_id!r}")
        return out


def _meta_from(f: h5py.File, path: Path) -> dict:
    attrs = dict(f.attrs)
    counts = attrs.get("sample_counts")
    return {
        "run_id": str(attrs.get("run_id", path.stem)),
        "name": str(attrs.get("name", "")),
        "operator": str(attrs.get("operator", "")),
        "notes": str(attrs.get("notes", "")),
        "t_start": float(attrs.get("t_start", 0.0)),
        "t_stop": float(attrs["t_stop"]) if "t_stop" in attrs else None,
        "channels_sha256": str(attrs.get("channels_sha256", "")),
        "sequence_sha256": str(attrs.get("sequence_sha256", "")),
        "interp_version": int(attrs.get("interp_version", 0)),
        "tbctl_git_hash": str(attrs.get("tbctl_git_hash", "")),
        "sample_counts": json.loads(counts) if counts else {},
        "size_bytes": path.stat().st_size,
    }
