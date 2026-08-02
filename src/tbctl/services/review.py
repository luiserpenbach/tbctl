"""Post-run review rules (Sift-style automated data review).

When a run stops, every configured rule (configs/review.yaml) is evaluated
over the recorded data and the result is stored in the run's HDF5 file as a
report — pass/fail per rule, so post-test review starts from a report card
instead of scrolling charts. Reports can be regenerated after editing the
rules (POST /api/runs/{id}/report/rerun).

This is a QA layer over recorded data only; live abort limits are redlines.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import h5py
import numpy as np

from tbctl.config.models import ReviewConfig

log = logging.getLogger(__name__)

REPORT_ATTR = "report_json"
_FIRE_END_EVENTS = {"seq_abort_commanded", "seq_terminal", "seq_reset", "run_stopped"}

_METRICS = {
    "max": lambda v: float(np.nanmax(v)),
    "min": lambda v: float(np.nanmin(v)),
    "mean": lambda v: float(np.nanmean(v)),
    "rms": lambda v: float(np.sqrt(np.nanmean(np.square(v)))),
}


def _events(f: h5py.File) -> list[dict]:
    if "events" not in f:
        return []
    return [json.loads(s) for s in f["events"].asstr()[:]]


def _fire_window(events: list[dict]) -> tuple[float, float | None] | None:
    """[seq_fired .. first terminal-ish event], None if the run never fired."""
    t0 = None
    for e in events:
        if e.get("event") == "seq_fired":
            t0 = float(e["time"])  # last FIRE wins if there were several
    if t0 is None:
        return None
    ends = [float(e["time"]) for e in events if e.get("event") in _FIRE_END_EVENTS]
    t1 = min((t for t in ends if t > t0), default=None)
    return (t0, t1)


def _run_sequence(events: list[dict]) -> str | None:
    """Name of the sequence this run fired (or loaded, as a fallback)."""
    name = None
    for e in events:
        if e.get("event") == "seq_fired" and e.get("sequence"):
            name = str(e["sequence"])
    if name is None:
        for e in events:
            if e.get("event") == "seq_loaded" and e.get("sequence"):
                name = str(e["sequence"])
    return name


def _series(f: h5py.File, tag: str) -> tuple[np.ndarray, np.ndarray] | None:
    for group in f["groups"]:
        g_tags = list(f["groups"][group].attrs["tags"])
        if tag in g_tags:
            col = g_tags.index(tag)
            return f["groups"][group]["t"][:], f["groups"][group]["data"][:, col]
    return None


def evaluate_run(review: ReviewConfig, path: Path) -> dict:
    """Evaluate all applicable rules against a closed run file."""
    results: list[dict] = []
    with h5py.File(path, "r") as f:
        events = _events(f)
        seq_name = _run_sequence(events)
        fire = _fire_window(events)
        for rule in review.rules:
            if rule.sequences and seq_name not in rule.sequences:
                continue  # scoped to other sequences — not part of this report
            row: dict = {
                "name": rule.name,
                "tag": rule.tag,
                "metric": rule.metric,
                "window": rule.window,
                "limit_min": rule.limit_min,
                "limit_max": rule.limit_max,
                "value": None,
                "passed": None,
                "skipped": None,
            }
            results.append(row)
            if rule.window == "fire" and fire is None:
                row["skipped"] = "run never fired a sequence"
                continue
            series = _series(f, rule.tag)
            if series is None:
                row["skipped"] = "channel not in this recording"
                continue
            t, v = series
            if rule.window == "fire":
                t0, t1 = fire  # type: ignore[misc]
                lo = int(np.searchsorted(t, t0, "left"))
                hi = len(t) if t1 is None else int(np.searchsorted(t, t1, "right"))
                vw = v[lo:hi]
                # A clean DONE run has no explicit engine-shutdown event, so the
                # fire window can end marginally past the last recorded sample
                # (event wall-clock vs sample-index timebase). Rather than
                # spuriously skip a rule with a whole run of data, fall back to
                # the full recording instead of an empty window.
                if len(vw) == 0:
                    log.warning(
                        "rule %s: empty fire window, evaluating over full recording", rule.name
                    )
                else:
                    v = vw
            if len(v) == 0 or bool(np.all(np.isnan(v))):
                row["skipped"] = "no samples in window"
                continue
            value = _METRICS[rule.metric](v)
            row["value"] = value
            row["passed"] = (rule.limit_min is None or value >= rule.limit_min) and (
                rule.limit_max is None or value <= rule.limit_max
            )

    summary = {
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"] is True),
        "failed": sum(1 for r in results if r["passed"] is False),
        "skipped": sum(1 for r in results if r["skipped"] is not None),
    }
    return {
        "generated_at": time.time(),
        "sequence": seq_name,
        "fire_window": list(fire) if fire else None,
        "results": results,
        "summary": summary,
    }


def store_report(path: Path, report: dict) -> None:
    # share the recorder's write lock: a run's HDF5 must never be opened "r+"
    # concurrently (report rerun vs. meta patch), h5py is not thread-safe
    from tbctl.services.recorder import HDF5_WRITE_LOCK

    with HDF5_WRITE_LOCK, h5py.File(path, "r+") as f:
        f.attrs[REPORT_ATTR] = json.dumps(report)


def load_report(path: Path) -> dict | None:
    with h5py.File(path, "r") as f:
        raw = f.attrs.get(REPORT_ATTR)
    return json.loads(raw) if raw else None
