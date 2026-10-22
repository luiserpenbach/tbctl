"""hopper-test-lab interchange: output state channels, run metadata, and the
CSV + channel-map export that hopper's Imports reads (docs/hopper-test-lab.md)."""

import asyncio
import csv
import io
import itertools
import json
import shutil

import h5py
import httpx
import pytest

from tbctl.cli import main as cli_main
from tbctl.node import build_app, build_context
from tbctl.services import hopper
from tests.conftest import CONFIGS

PULSE_SEQ = {
    "name": "pulse_test",
    "description": "purge pulse for export tests",
    "test_type": "igniter",
    "max_duration_s": 1.0,
    "preconditions": [],
    "steps": [
        {"t": 0.0, "set": {"valve.purge": "open"}},
        {"t": 0.3, "set": {"valve.purge": "close"}},
    ],
    "abort": [{"t": 0.0, "set": {"valve.purge": "close"}}],
    "allow_unsafe_end": False,
    "redlines": [],
}


@pytest.fixture
async def client(tmp_path):
    cfg = tmp_path / "configs"
    shutil.copytree(CONFIGS, cfg)
    ctx = build_context(cfg, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.ctx = ctx
            yield c


async def _pulse_run(client) -> str:
    client.ctx.iface.sim_set("pressure.chamber", 12.0, noise_sigma_eu=0.0)
    await client.post("/api/runs/start", json={"name": "pulse", "operator": "op", "notes": ""})
    await asyncio.sleep(0.3)
    assert (await client.post("/api/sequences/pulse_test", json=PULSE_SEQ)).status_code == 200
    await client.post("/api/control-mode", json={"enabled": True})
    assert (await client.post("/api/sequences/pulse_test/load")).status_code == 200
    assert (
        await client.post("/api/sequences/arm", json={"confirm": "pulse_test"})
    ).status_code == 200
    assert (
        await client.post("/api/sequences/fire", json={"confirm": "pulse_test"})
    ).status_code == 200
    for _ in range(50):
        if (await client.get("/api/status")).json()["sequencer"]["state"] == "DONE":
            break
        await asyncio.sleep(0.1)
    await client.post("/api/sequences/reset")
    await client.post("/api/control-mode", json={"enabled": False})
    await asyncio.sleep(0.3)
    return (await client.post("/api/runs/stop")).json()["run_id"]


async def test_hopper_csv_export(client):
    run_id = await _pulse_run(client)
    r = await client.get(f"/api/runs/{run_id}/export/hopper.csv")
    assert r.status_code == 200
    lines = r.text.splitlines()
    comments = [line for line in lines if line.startswith("#")]
    assert lines[: len(comments)] == comments  # run details come first
    details = dict(line[2:].split(": ", 1) for line in comments[1:])
    assert details["run_id"] == run_id and details["test_type"] == "igniter"
    assert details["sequence"] == "pulse_test" and details["operator"] == "op"
    assert "notes" not in details  # empty values are left out
    rows = list(csv.reader(io.StringIO("\n".join(lines[len(comments) :]))))
    header, data = rows[0], rows[1:]
    assert header[:2] == ["timestamp", "testcaseId"]
    tags = header[2:]
    assert {"pressure.chamber", "calc.dp_ox_inj", "valve.purge", "igniter.main"} <= set(tags)

    # epoch milliseconds (hopper reads > 1e11 as absolute), strictly increasing
    ts = [float(row[0]) for row in data]
    assert ts[0] > 1e11 and all(b > a for a, b in itertools.pairwise(ts))
    assert {row[1] for row in data} == {run_id}

    col = {tag: 2 + i for i, tag in enumerate(tags)}
    pc = [float(row[col["pressure.chamber"]]) for row in data if row[col["pressure.chamber"]]]
    assert pc and pc[-1] == pytest.approx(12.0, rel=0.05)

    # the sequence's purge pulse is a measured 0/1 trace with one ~0.3 s pulse
    purge = [float(row[col["valve.purge"]]) for row in data]
    assert set(purge) == {0.0, 1.0}
    rising = [ts[i] for i in range(1, len(purge)) if purge[i - 1] < 0.5 <= purge[i]]
    falling = [ts[i] for i in range(1, len(purge)) if purge[i - 1] >= 0.5 > purge[i]]
    assert len(rising) == len(falling) == 1
    assert falling[0] - rising[0] == pytest.approx(300, abs=45)  # 50 Hz stream

    meta = (await client.get(f"/api/runs/{run_id}")).json()
    assert meta["sequence"] == "pulse_test" and meta["test_type"] == "igniter"


async def test_hopper_channel_map(client):
    run_id = await _pulse_run(client)
    body = (await client.get(f"/api/runs/{run_id}/export/hopper-channel-map.json")).json()
    assert body["testType"] == "any" and body["name"].endswith(run_id)
    ch = body["channels"]
    assert ch["pressure.chamber"] == {
        "daqId": "pressure.chamber",
        "name": "PT-CC-01",
        "unit": "bar",
        "class": "pressure",
        "color": ch["pressure.chamber"]["color"],
        "visible": True,
    }
    assert ch["valve.purge"]["class"] == ch["igniter.main"]["class"] == "valve"
    assert ch["temp.injector"]["class"] == "temperature" and ch["temp.injector"]["unit"] == "°C"
    assert ch["force.thrust"]["class"] == "unknown"
    # computed on the node -> plain data in hopper, never hopper's "derived"
    assert ch["calc.dp_ox_inj"]["class"] == "pressure"
    assert all(c["class"] != "derived" for c in ch.values())


async def test_export_rejects_unknown_and_active_runs(client):
    assert (await client.get("/api/runs/nope/export/hopper.csv")).status_code == 404
    run = (
        await client.post("/api/runs/start", json={"name": "a", "operator": "op", "notes": ""})
    ).json()
    r = await client.get(f"/api/runs/{run['run_id']}/export/hopper-channel-map.json")
    assert r.status_code == 409
    await client.post("/api/runs/stop")


async def test_legacy_run_file(client):
    """Schema-1 files: no channels_json, outcome 'good' -> hopper 'pass'."""
    run_id = await _pulse_run(client)
    path = client.ctx.recorder.run_path(run_id)
    with h5py.File(path, "r+") as f:
        del f.attrs["channels_json"]
        f.attrs["user_meta"] = json.dumps({"outcome": "good"})
        f.attrs["notes"] = "line one\nline two"
    assert (await client.get(f"/api/runs/{run_id}")).json()["outcome"] == "pass"
    ch = hopper.run_channel_map(path)["channels"]
    assert ch["pressure.chamber"]["name"] == "pressure.chamber"
    assert ch["pressure.chamber"]["class"] == "unknown"
    text = "".join(hopper.csv_rows(path))
    assert "\ntimestamp,testcaseId," in text and "# test_type: igniter" in text
    assert "# outcome: pass\n" in text  # legacy 'good', in hopper's vocabulary
    assert "# notes: line one line two\n" in text  # one line per value


async def test_cli_export(client, tmp_path):
    run_id = await _pulse_run(client)
    path = client.ctx.recorder.run_path(run_id)
    out = tmp_path / "export"
    assert cli_main(["export-hopper", str(path), "--out", str(out)]) == 0
    assert "\ntimestamp,testcaseId," in (out / f"{run_id}.hopper.csv").read_text()
    cmap = json.loads((out / f"{run_id}.channel-map.json").read_text())
    assert "valve.purge" in cmap["channels"]


def test_output_states_can_be_disabled(tmp_path):
    from tbctl.config.loader import load_channels
    from tbctl.hw.ljm_mock import MockDevice
    from tbctl.services.acquisition import AcquisitionService

    p = tmp_path / "channels.yaml"
    p.write_text(
        (CONFIGS / "channels.yaml")
        .read_text()
        .replace("record_outputs: true", "record_outputs: false")
    )
    chmap = load_channels(p).model
    acq = AcquisitionService(chmap, MockDevice(chmap))
    assert not any(t.startswith(("valve.", "igniter.")) for t in acq.rings["default"].tags)


def test_dummy_dio_samples_decode_as_unknown(tmp_path):
    import numpy as np

    from tbctl.config.loader import load_channels
    from tbctl.hw.ljm_mock import MockDevice
    from tbctl.services.acquisition import AcquisitionService

    chmap = load_channels(CONFIGS / "channels.yaml").model
    acq = AcquisitionService(chmap, MockDevice(chmap))
    ring = acq.rings["default"]
    n_in = len(chmap.inputs())
    volts = np.zeros((2, n_in + 2))  # inputs, DIO word, T7 temp sensor (CJ)
    volts[0, n_in] = 1 << chmap.bit_index("valve.ox_main")  # ox_main open (active high)
    volts[1, n_in] = -9999.0  # LJM dummy sample
    acq._ingest(volts)
    _t, v = ring.window(-1.0)  # unstarted stream: sample times start at epoch 0
    col = ring.columns(["valve.ox_main"])[0]
    assert v[0, col] == 1.0 and np.isnan(v[1, col])
