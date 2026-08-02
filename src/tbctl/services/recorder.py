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
import threading
import time
from collections.abc import Callable
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
# Cap the in-RAM pending buffer per group so a stuck flush (disk full, I/O
# error) cannot grow memory without bound for the rest of a run — past this we
# drop the oldest blocks and account for the loss instead of OOMing the node.
MAX_PENDING_BLOCKS = 4096

# h5py is not thread-safe and a single run file must never be opened "r+"
# concurrently (meta patch vs. report store, both on FastAPI threadpool
# threads). Serialize all post-hoc writers through one process-wide lock.
HDF5_WRITE_LOCK = threading.Lock()


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
        self._pending_lock = threading.Lock()  # _on_block (loop) vs _flush (thread)
        self._start_stop_lock = asyncio.Lock()  # no concurrent start/stop races
        self._data_lost = 0
        # list_runs metadata cache: path -> (mtime, size, meta); avoids
        # re-opening every run file on every listing
        self._meta_cache: dict[Path, tuple[float, int, dict]] = {}
        self._unsubscribe = None
        self._flush_task: asyncio.Task | None = None
        self.sequence_sha256: str = ""  # set by the sequencer when a table is loaded
        # called (blocking, in a thread) with the RunInfo after the file is
        # closed — used for post-run review report generation
        self.on_stop: Callable[[RunInfo], None] | None = None

    @property
    def active(self) -> RunInfo | None:
        return self._run

    # -- lifecycle ---------------------------------------------------------

    async def start_run(self, name: str, operator: str, notes: str = "") -> RunInfo:
        async with self._start_stop_lock:
            return await self._start_run_locked(name, operator, notes)

    async def _start_run_locked(self, name: str, operator: str, notes: str) -> RunInfo:
        if self._run is not None:
            raise RecorderError(f"run {self._run.run_id} already recording")
        safe = re.sub(r"[^a-zA-Z0-9_\-]+", "_", name).strip("_") or "run"
        t0 = time.time()
        run_id = f"{datetime.fromtimestamp(t0, UTC):%Y%m%d-%H%M%S}-{safe}"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        path = self._data_dir / f"{run_id}.h5"
        # a same-second run_id collision would otherwise 500 with FileExistsError
        try:
            f = await asyncio.to_thread(h5py.File, path, "x")
        except (FileExistsError, OSError) as e:
            raise RecorderError(f"cannot create run file {path.name}: {e}") from e
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
        self._data_lost = 0
        self._unsubscribe = self._acq.add_listener(self._on_block)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="recorder-flush")
        log.info("run %s started -> %s", run_id, path)
        return self._run

    async def stop_run(self) -> RunInfo:
        async with self._start_stop_lock:
            return await self._stop_run_locked()

    async def _stop_run_locked(self) -> RunInfo:
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
        # The file MUST end up closed and state cleared even if the final flush
        # or attribute writes fail (e.g. disk full) — otherwise self._run stays
        # set and the node can never start another run.
        f = self._file
        run.t_stop = time.time()
        try:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._flush)
            if f is not None:
                for group in self._acq.rings:
                    with contextlib.suppress(Exception):
                        run.sample_counts[group] = f["groups"][group]["t"].shape[0]
                with contextlib.suppress(Exception):
                    f.attrs["t_stop"] = run.t_stop
                    f.attrs["t_stop_utc"] = datetime.fromtimestamp(run.t_stop, UTC).isoformat()
                    f.attrs["sample_counts"] = json.dumps(run.sample_counts)
        finally:
            if f is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(f.close)
            self._file = None
            self._run = None
        if self._data_lost:
            log.error(
                "run %s lost %d buffered blocks to flush failures", run.run_id, self._data_lost
            )
        log.info("run %s stopped (%s samples)", run.run_id, run.sample_counts)
        if self.on_stop is not None:
            try:
                await asyncio.to_thread(self.on_stop, run)
            except Exception:
                log.exception("post-stop hook failed for %s", run.run_id)
        return run

    # -- data & event ingest ----------------------------------------------

    def _on_block(self, group: str, t: np.ndarray, block: np.ndarray) -> None:
        with self._pending_lock:
            buf = self._pending.setdefault(group, [])
            buf.append((t, block))
            # bound memory if the flush thread is wedged (disk full / I/O error)
            if len(buf) > MAX_PENDING_BLOCKS:
                dropped = buf[: len(buf) - MAX_PENDING_BLOCKS]
                del buf[: len(buf) - MAX_PENDING_BLOCKS]
                self._data_lost += sum(len(b[0]) for b in dropped)

    def on_event(self, record: dict) -> None:
        """EventLog sink: mirror events into the active run file."""
        if self._run is not None:
            with self._pending_lock:
                self._pending_events.append(json.dumps(record))

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_S)
            # A flush error (disk full, I/O) must NOT kill this task — that
            # would let _pending grow unbounded and leave the run unstoppable.
            try:
                await asyncio.to_thread(self._flush)
            except Exception:
                log.exception("recorder flush failed; will retry next tick")

    def _flush(self) -> None:
        f = self._file
        if f is None:
            return
        with self._pending_lock:
            pending = {g: b for g, b in self._pending.items() if b}
            for g in pending:
                self._pending[g] = []
            events, self._pending_events = self._pending_events, []
        for group, blocks in pending.items():
            t = np.concatenate([b[0] for b in blocks])
            v = np.concatenate([b[1] for b in blocks])
            g = f["groups"][group]
            n0 = g["t"].shape[0]
            g["t"].resize((n0 + len(t),))
            g["t"][n0:] = t
            g["data"].resize((n0 + len(t), v.shape[1]))
            g["data"][n0:] = v
        if events:
            d = f["events"]
            n0 = d.shape[0]
            d.resize((n0 + len(events),))
            d[n0:] = events
        f.flush()

    # -- historical access -------------------------------------------------

    def list_runs(self) -> list[dict]:
        out = []
        seen: set[Path] = set()
        for path in sorted(self._data_dir.glob("*.h5"), reverse=True):
            if self._run and path == self._run.path:
                continue  # active run listed via status
            seen.add(path)
            try:
                st = path.stat()
                cached = self._meta_cache.get(path)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    out.append(cached[2])
                    continue
                with h5py.File(path, "r") as f:
                    meta = _meta_from(f, path)
                self._meta_cache[path] = (st.st_mtime, st.st_size, meta)
                out.append(meta)
            except OSError:
                log.warning("unreadable run file %s", path)
        # evict cache entries for deleted files
        for gone in set(self._meta_cache) - seen:
            self._meta_cache.pop(gone, None)
        return out

    def run_meta(self, run_id: str) -> dict:
        path = self._run_path(run_id)
        with h5py.File(path, "r") as f:
            return _meta_from(f, path)

    def run_path(self, run_id: str) -> Path:
        """Resolve a closed run's file path (KeyError / RecorderError)."""
        return self._run_path(run_id)

    def _run_path(self, run_id: str) -> Path:
        if not re.match(r"^[a-zA-Z0-9_\-]+$", run_id):
            raise KeyError(f"bad run id {run_id!r}")
        path = self._data_dir / f"{run_id}.h5"
        if not path.is_file():
            raise KeyError(f"unknown run {run_id!r}")
        if self._run and path == self._run.path:
            raise RecorderError("run is still recording")
        return path

    def update_user_meta(
        self, run_id: str, tags: list[str] | None, outcome: str | None, clear_outcome: bool
    ) -> dict:
        """Patch the mutable review metadata (tags / outcome) of a closed run.
        Stored as a JSON attr in the run file; None leaves a field untouched."""
        path = self._run_path(run_id)
        with HDF5_WRITE_LOCK, h5py.File(path, "r+") as f:
            current = {}
            if raw := f.attrs.get("user_meta"):
                with contextlib.suppress(Exception):
                    current = json.loads(raw)
            if tags is not None:
                current["tags"] = [t.strip()[:40] for t in tags if t.strip()][:20]
            if clear_outcome:
                current.pop("outcome", None)
            elif outcome is not None:
                current["outcome"] = outcome
            f.attrs["user_meta"] = json.dumps(current)
        self._meta_cache.pop(path, None)
        return current

    def run_events(self, run_id: str) -> list[dict]:
        """Events mirrored into the run file (notes, seq_*, redline_tripped...)."""
        path = self._run_path(run_id)
        with h5py.File(path, "r") as f:
            if "events" not in f:
                return []
            return [json.loads(s) for s in f["events"].asstr()[:]]

    def export_csv_rows(
        self,
        run_id: str,
        tags: list[str],
        t0: float | None = None,
        t1: float | None = None,
    ):
        """Yield CSV lines: wide table on the union of the tags' timebases
        (cells blank where a slower rate group has no sample). Full
        resolution — no decimation."""
        path = self._run_path(run_id)
        series: list[tuple[np.ndarray, np.ndarray]] = []
        with h5py.File(path, "r") as f:
            for tag in tags:
                for group in f["groups"]:
                    g_tags = list(f["groups"][group].attrs["tags"])
                    if tag in g_tags:
                        col = g_tags.index(tag)
                        t = f["groups"][group]["t"][:]
                        lo = int(np.searchsorted(t, t0, "left")) if t0 is not None else 0
                        hi = int(np.searchsorted(t, t1, "right")) if t1 is not None else len(t)
                        series.append((t[lo:hi], f["groups"][group]["data"][lo:hi, col]))
                        break
                else:
                    raise KeyError(f"tag {tag!r} not in run {run_id!r}")

        # Stream the wide table by k-way merge over the per-series timelines
        # instead of allocating a dense (len(union) by len(tags)) object matrix,
        # which for a long multi-rate run is hundreds of MB of Python strings
        # and can OOM the control node.
        import heapq

        yield "t_epoch," + ",".join(tags) + "\n"
        ptr = [0] * len(series)
        lens = [len(t) for t, _ in series]
        heap = [(float(series[j][0][0]), j) for j in range(len(series)) if lens[j] > 0]
        heapq.heapify(heap)
        chunk: list[str] = []
        while heap:
            cur_t = heap[0][0]
            row = [""] * len(tags)
            while heap and heap[0][0] == cur_t:
                _, j = heapq.heappop(heap)
                p = ptr[j]
                row[j] = repr(float(series[j][1][p]))
                ptr[j] += 1
                if ptr[j] < lens[j]:
                    heapq.heappush(heap, (float(series[j][0][ptr[j]]), j))
            chunk.append(f"{cur_t:.6f}," + ",".join(row) + "\n")
            if len(chunk) >= 2000:
                yield "".join(chunk)
                chunk = []
        if chunk:
            yield "".join(chunk)

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
    schema = int(attrs.get("schema_version", 0))
    if schema > SCHEMA_VERSION:
        log.warning(
            "run %s written with schema %d, newer than supported %d; metadata may be partial",
            path.name,
            schema,
            SCHEMA_VERSION,
        )
    counts = attrs.get("sample_counts")
    report_summary = None
    if raw := attrs.get("report_json"):
        with contextlib.suppress(Exception):
            report_summary = json.loads(raw).get("summary")
    user_meta: dict = {}
    if raw := attrs.get("user_meta"):
        with contextlib.suppress(Exception):
            user_meta = json.loads(raw)
    return {
        "report_summary": report_summary,
        "tags": user_meta.get("tags", []),
        "outcome": user_meta.get("outcome"),
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
        "schema_version": schema,
        "size_bytes": path.stat().st_size,
    }
