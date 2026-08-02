"""Run annotations: time-range marks on recorded runs with a triage status.

Stored in a JSON sidecar next to the run's HDF5 (`<run_id>.annotations.json`)
because annotations are mutable review artifacts — status flips, labels get
edited — and HDF5 is the wrong place for in-place mutation. The HDF5 stays
the immutable source of truth for the data itself.

Failed review rules automatically create annotations (source="rule"); those
are replaced wholesale whenever the report is (re)generated, so a fixed rule
never leaves a stale FAIL mark behind. User annotations are never touched.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import h5py

SEVERITIES = ("info", "warn", "crit")
STATUSES = ("open", "resolved")

# The annotation endpoints are sync `def`, so Starlette runs them concurrently
# in a threadpool. Serialize the read-modify-write of each sidecar so two
# concurrent mutations can't lose an annotation (last-writer-wins) and a reader
# can't observe a half-written file. One process-wide lock is ample here.
_SIDECAR_LOCK = threading.Lock()


def _sidecar(run_h5: Path) -> Path:
    return run_h5.with_name(run_h5.stem + ".annotations.json")


def _load(run_h5: Path) -> list[dict]:
    p = _sidecar(run_h5)
    if not p.is_file():
        return []
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _save(run_h5: Path, items: list[dict]) -> None:
    # atomic write: a concurrent reader sees either the old or the new file,
    # never a truncated one
    p = _sidecar(run_h5)
    tmp = p.with_name(p.name + f".tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(items, indent=1))
    os.replace(tmp, p)


def list_annotations(run_h5: Path) -> list[dict]:
    return sorted(_load(run_h5), key=lambda a: a["t0"])


def add_annotation(
    run_h5: Path,
    t0: float,
    t1: float,
    label: str,
    severity: str = "warn",
    source: str = "user",
) -> dict:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    if t1 < t0:
        t0, t1 = t1, t0
    now = time.time()
    ann = {
        "id": uuid.uuid4().hex[:12],
        "t0": float(t0),
        "t1": float(t1),
        "label": str(label)[:200],
        "severity": severity,
        "status": "open",
        "source": source,
        "created_at": now,
        "updated_at": now,
    }
    with _SIDECAR_LOCK:
        items = _load(run_h5)
        items.append(ann)
        _save(run_h5, items)
    return ann


def update_annotation(run_h5: Path, aid: str, patch: dict) -> dict:
    with _SIDECAR_LOCK:
        items = _load(run_h5)
        for ann in items:
            if ann["id"] == aid:
                if "label" in patch and patch["label"] is not None:
                    ann["label"] = str(patch["label"])[:200]
                if "severity" in patch and patch["severity"] is not None:
                    if patch["severity"] not in SEVERITIES:
                        raise ValueError(f"severity must be one of {SEVERITIES}")
                    ann["severity"] = patch["severity"]
                if "status" in patch and patch["status"] is not None:
                    if patch["status"] not in STATUSES:
                        raise ValueError(f"status must be one of {STATUSES}")
                    ann["status"] = patch["status"]
                ann["updated_at"] = time.time()
                _save(run_h5, items)
                return ann
    raise KeyError(f"no annotation {aid!r}")


def delete_annotation(run_h5: Path, aid: str) -> None:
    with _SIDECAR_LOCK:
        items = _load(run_h5)
        kept = [a for a in items if a["id"] != aid]
        if len(kept) == len(items):
            raise KeyError(f"no annotation {aid!r}")
        _save(run_h5, kept)


def replace_rule_annotations(run_h5: Path, report: dict) -> int:
    """Drop all source='rule' annotations and re-create one per failed rule.
    Returns the number created."""
    with h5py.File(run_h5, "r") as f:
        t_start = float(f.attrs.get("t_start", 0.0))
        t_stop = float(f.attrs.get("t_stop", t_start))
    fire = report.get("fire_window")
    with _SIDECAR_LOCK:
        items = [a for a in _load(run_h5) if a.get("source") != "rule"]
        created = 0
        now = time.time()
        for r in report.get("results", []):
            if r.get("passed") is not False:
                continue
            if r.get("window") == "fire" and fire:
                t0, t1 = float(fire[0]), float(fire[1] if fire[1] is not None else t_stop)
            else:
                t0, t1 = t_start, t_stop
            limit = (
                f"≤ {r['limit_max']}"
                if r.get("limit_max") is not None and r.get("limit_min") is None
                else f"≥ {r['limit_min']}"
                if r.get("limit_min") is not None and r.get("limit_max") is None
                else f"{r.get('limit_min')}…{r.get('limit_max')}"
            )
            items.append(
                {
                    "id": uuid.uuid4().hex[:12],
                    "t0": t0,
                    "t1": t1,
                    "label": f"rule {r['name']}: {r['metric']}({r['tag']}) = "
                    f"{r['value']:.3g}, limit {limit}",
                    "severity": "crit",
                    "status": "open",
                    "source": "rule",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            created += 1
        _save(run_h5, items)
    return created
