"""hopper-test-lab interchange: export a recorded run in the exact shape
hopper-test-lab imports, plus a matching channel map.

Contract (docs/hopper-test-lab.md):

- Wide CSV, header `timestamp,testcaseId,<tag>,...`. `timestamp` is epoch
  milliseconds (hopper reads values > 1e11 as absolute epoch ms, so the
  test date is right); `testcaseId` is the run id.
- One column per recorded channel, keyed by tag (hopper's "DAQ ID"), on the
  union timebase of all rate groups. Cells are blank where a slower group
  has no sample; hopper holds the last value.
- Outputs (valve / igniter) are 0/1 open/on columns, class "valve" in the
  channel map: hopper times ignition delay from their rising edges.
- Derived channels are exported as measured data, never as hopper class
  "derived" (hopper recomputes those from its own expressions and drops
  them when it re-reads the raw file).
- The channel map is the POST body of hopper's `/api/channel-maps`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path

import h5py
import numpy as np

from tbctl.services.recorder import union_rows

# hopper-test-lab src/lib/colors.ts, so exported maps look native there
TRACE_COLORS = [
    "#e8871e",
    "#2f8fd8",
    "#8b5cf6",
    "#16a34a",
    "#e11d48",
    "#0891b2",
    "#c026d3",
    "#65a30d",
    "#ea580c",
    "#4f46e5",
    "#0d9488",
    "#b45309",
]

_UNIT_CLASS = {
    **dict.fromkeys(
        ["bar", "bara", "barg", "mbar", "psi", "psia", "psig", "pa", "kpa", "mpa"], "pressure"
    ),
    **dict.fromkeys(["degc", "degf", "k", "°c", "°f"], "temperature"),
    **dict.fromkeys(["g/s", "kg/s", "g/min", "kg/h", "l/min", "slpm", "sccm"], "flow"),
}
_UNIT_LABEL = {"degC": "°C", "degF": "°F"}


def hopper_class(ch: dict) -> str:
    """hopper ChannelClass for a channel description (recorder.describe_channels)."""
    kind = ch.get("kind", "")
    if kind in ("valve", "igniter"):
        return "valve"
    if kind == "thermocouple_k":
        return "temperature"
    return _UNIT_CLASS.get(str(ch.get("unit", "")).strip().lower(), "unknown")


def channel_map(channels: list[dict], name: str) -> dict:
    """hopper ChannelMap (POST /api/channel-maps body) for these channels."""
    out = {}
    for i, ch in enumerate(channels):
        tag = ch["tag"]
        unit = str(ch.get("unit", ""))
        out[tag] = {
            "daqId": tag,
            "name": ch.get("name") or tag,
            "unit": _UNIT_LABEL.get(unit, unit),
            "class": hopper_class(ch),
            "color": TRACE_COLORS[i % len(TRACE_COLORS)],
            "visible": True,
        }
    return {"name": name, "testType": "any", "channels": out}


def _run_channels(f: h5py.File) -> list[dict]:
    """Recorded channels in config order, described as in describe_channels.
    Run files older than schema 2 carry no description: tag only."""
    recorded = [str(t) for g in f["groups"].values() for t in g.attrs["tags"]]
    described = {}
    if raw := f.attrs.get("channels_json"):
        described = {c["tag"]: c for c in json.loads(raw)}
    ordered = [t for t in described if t in recorded] + [t for t in recorded if t not in described]
    return [described.get(t, {"tag": t, "name": "", "kind": "", "unit": ""}) for t in ordered]


def run_channel_map(path: Path) -> dict:
    with h5py.File(path, "r") as f:
        name = f"tbctl {f.attrs.get('run_id', path.stem)}"
        return channel_map(_run_channels(f), name)


def _cell(v: float) -> str:
    return "" if math.isnan(v) else repr(float(v))


def csv_rows(path: Path) -> Iterator[str]:
    """hopper-test-lab wide CSV for a closed run file, full resolution."""
    with h5py.File(path, "r") as f:
        run_id = str(f.attrs.get("run_id", path.stem))
        tags = [c["tag"] for c in _run_channels(f)]
        where = {}
        for g in f["groups"].values():
            for col, tag in enumerate(g.attrs["tags"]):
                where[str(tag)] = (g, col)
        series: list[tuple[np.ndarray, np.ndarray]] = []
        for tag in tags:
            g, col = where[tag]
            series.append((g["t"][:], g["data"][:, col]))
    yield "timestamp,testcaseId," + ",".join(tags) + "\n"
    prefix = f",{run_id},"
    yield from union_rows(series, lambda t: f"{t * 1000.0:.3f}{prefix}", _cell)
