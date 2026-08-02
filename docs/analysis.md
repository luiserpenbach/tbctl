# Offline analysis of tbctl runs

Every run is a self-contained HDF5 file in `data/runs/` (plus a
`.annotations.json` sidecar for review annotations). `tools/tbctl_load.py`
reads one back into plain numpy structures — no pandas dependency, but one
line away from a DataFrame.

## Quick look

```bash
python tools/tbctl_load.py data/runs/20260802-201227-hot.h5
```

## In a notebook

```python
from tools.tbctl_load import load_run
import pandas as pd

run = load_run("data/runs/20260802-201227-hot.h5")

# per-rate-group DataFrame (t is epoch seconds)
df = pd.DataFrame(run.groups["default"]).set_index("t")
df["pressure.chamber"].plot()

# slice to the fire window (seq_fired .. sequence end)
t0, t1 = run.fire_window()
fire = df.loc[t0:t1]
print(fire["pressure.chamber"].max())

# provenance & review artifacts
run.meta["channels_sha256"], run.meta["sequence_sha256"]
run.report["summary"]          # post-run review report (pass/fail per rule)
run.annotations                # review annotations incl. auto rule-failures
run.events                     # full event log (valve cmds, FIRE, notes, ...)
```

## Alternative exports

- CSV: `GET /api/runs/{id}/export.csv?tags=a,b&t0=&t1=` (full resolution,
  union timebase) — also linked from each run in the sidebar.
- Raw HDF5: the file itself is the source of truth; `h5py.File(path)` layout:
  `/groups/<rate_group>/{t,data}` with a `tags` attr, `/events` (JSON lines),
  root attrs for metadata (`report_json`, `user_meta` for tags/outcome).
