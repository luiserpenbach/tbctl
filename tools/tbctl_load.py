"""Load a tbctl run file into plain Python/numpy structures for analysis.

No dependencies beyond h5py + numpy (both already required by tbctl).
Convert to pandas in one line if you want DataFrames — see docs/analysis.md.

Usage:
    from tools.tbctl_load import load_run
    run = load_run("data/runs/20260802-201227-hot.h5")
    run.meta["operator"], run.meta["tags"]
    t = run.groups["default"]["t"]              # epoch seconds (f8)
    pc = run.groups["default"]["pressure.chamber"]
    run.groups["default"]["valve.ox_main"]       # outputs: 0/1 open (schema >= 2)
    run.events                                   # list of dicts (JSONL mirror)
    run.report                                   # post-run review report or None
    run.annotations                              # review annotations (sidecar)
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import h5py
import numpy as np


@dataclass
class Run:
    path: Path
    meta: dict
    groups: dict[str, dict[str, np.ndarray]]  # group -> {"t": ..., "<tag>": ...}
    events: list[dict]
    report: dict | None
    annotations: list[dict] = field(default_factory=list)

    def channel(self, tag: str) -> tuple[np.ndarray, np.ndarray]:
        """(t, values) for a tag, wherever it lives."""
        for cols in self.groups.values():
            if tag in cols:
                return cols["t"], cols[tag]
        raise KeyError(f"tag {tag!r} not in run")

    def fire_window(self) -> tuple[float, float] | None:
        """[seq_fired .. sequence end] epoch seconds, None if never fired."""
        t0 = None
        for e in self.events:
            if e.get("event") == "seq_fired":
                t0 = float(e["time"])
        if t0 is None:
            return None
        ends = [
            float(e["time"])
            for e in self.events
            if e.get("event") in ("seq_abort_commanded", "seq_terminal", "seq_reset")
            and float(e["time"]) > t0
        ]
        t1 = min(ends) if ends else float(self.meta.get("t_stop") or t0)
        return (t0, t1)


def load_run(path: str | Path) -> Run:
    path = Path(path)
    with h5py.File(path, "r") as f:
        meta = {}
        for k, v in f.attrs.items():
            if k in ("report_json", "user_meta"):
                continue
            meta[k] = v.item() if hasattr(v, "item") else v
        if raw := f.attrs.get("user_meta"):
            with contextlib.suppress(Exception):
                meta.update(json.loads(raw))
        meta.setdefault("tags", [])
        # outcomes follow hopper-test-lab (pass/anomaly/fail/void); old files: good/scrub
        meta["outcome"] = {"good": "pass", "scrub": "void"}.get(
            meta.get("outcome"), meta.get("outcome")
        )

        groups: dict[str, dict[str, np.ndarray]] = {}
        for name in f["groups"]:
            g = f["groups"][name]
            tags = list(g.attrs["tags"])
            data = g["data"][:]
            cols = {"t": g["t"][:]}
            for i, tag in enumerate(tags):
                cols[tag] = data[:, i]
            groups[name] = cols

        events = [json.loads(s) for s in f["events"].asstr()[:]] if "events" in f else []
        report = None
        if raw := f.attrs.get("report_json"):
            with contextlib.suppress(Exception):
                report = json.loads(raw)

    ann_path = path.with_name(path.stem + ".annotations.json")
    annotations = json.loads(ann_path.read_text()) if ann_path.is_file() else []
    return Run(path, meta, groups, events, report, annotations)


if __name__ == "__main__":
    import sys

    run = load_run(sys.argv[1])
    print(f"run {run.meta.get('run_id')}  operator={run.meta.get('operator')}")
    print(f"tags={run.meta.get('tags')} outcome={run.meta.get('outcome')}")
    for name, cols in run.groups.items():
        tags = [k for k in cols if k != "t"]
        print(f"  group {name}: {len(cols['t'])} samples x {len(tags)} channels")
    if run.report:
        s = run.report["summary"]
        print(f"  report: {s['passed']} passed / {s['failed']} failed / {s['skipped']} skipped")
    print(f"  {len(run.events)} events, {len(run.annotations)} annotations")
