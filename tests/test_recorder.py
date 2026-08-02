"""Recorder: HDF5 layout, metadata attrs, sample accounting, historical reads."""

import asyncio
import json

import h5py
import pytest

from tbctl.config.loader import load_channels
from tbctl.hw.ljm_mock import MockDevice
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.events import EventLog
from tbctl.services.recorder import Recorder, RecorderError
from tests.conftest import CONFIGS


@pytest.fixture
def chmap_loaded():
    return load_channels(CONFIGS / "channels.yaml")


@pytest.fixture
async def rig(chmap_loaded, tmp_path):
    mock = MockDevice(chmap_loaded.model)
    mock.open()
    mock.sim_set("pressure.chamber", 10.0, noise_sigma_eu=0.0)
    acq = AcquisitionService(chmap_loaded.model, mock)
    rec = Recorder(chmap_loaded.model, chmap_loaded.sha256, acq, tmp_path / "runs", "deadbeef")
    events = EventLog(tmp_path / "logs")
    events.add_sink(rec.on_event)
    await acq.start()
    yield mock, acq, rec, events
    await acq.stop()


async def test_record_and_reopen(rig):
    _mock, _acq, rec, events = rig
    run = await rec.start_run("coldflow test", "luis", "first shakedown")
    events.log("arm", sequence="coldflow_v1")
    await asyncio.sleep(1.2)
    stopped = await rec.stop_run()

    assert stopped.run_id == run.run_id
    assert stopped.sample_counts["default"] > 30  # ~1.2 s @ 50 Hz

    with h5py.File(run.path, "r") as f:
        assert f.attrs["schema_version"] == 1
        assert f.attrs["operator"] == "luis"
        assert f.attrs["tbctl_git_hash"] == "deadbeef"
        assert len(f.attrs["channels_sha256"]) == 64
        assert f.attrs["interp_version"] == 1
        g = f["groups"]["default"]
        assert list(g.attrs["tags"])
        assert g["t"].shape[0] == g["data"].shape[0] == stopped.sample_counts["default"]
        # timestamps strictly increasing at ~50 Hz
        t = g["t"][:]
        assert ((t[1:] - t[:-1]) > 0).all()
        # events mirrored into the file
        evs = [json.loads(e) for e in f["events"][:]]
        assert any(e["event"] == "arm" for e in evs)

    # historical API
    metas = rec.list_runs()
    assert metas[0]["run_id"] == run.run_id
    assert metas[0]["sample_counts"]["default"] > 30
    data = rec.run_data(run.run_id, ["pressure.chamber"], max_points=50)
    series = data["pressure.chamber"]
    assert 0 < len(series["t"]) <= 50
    assert series["mean"][-1] == pytest.approx(10.0, rel=0.1)


async def test_double_start_rejected(rig):
    _mock, _acq, rec, _events = rig
    await rec.start_run("a", "op")
    with pytest.raises(RecorderError, match="already recording"):
        await rec.start_run("b", "op")
    await rec.stop_run()


async def test_stop_without_start(rig):
    _mock, _acq, rec, _events = rig
    with pytest.raises(RecorderError, match="no active run"):
        await rec.stop_run()


async def test_active_run_not_readable(rig):
    _mock, _acq, rec, _events = rig
    run = await rec.start_run("live", "op")
    with pytest.raises(RecorderError, match="still recording"):
        rec.run_data(run.run_id, ["pressure.chamber"])
    await rec.stop_run()


async def test_unknown_run(rig):
    _mock, _acq, rec, _events = rig
    with pytest.raises(KeyError):
        rec.run_meta("nope-123")
    with pytest.raises(KeyError):
        rec.run_data("nope-123", ["pressure.chamber"])


def test_event_log_jsonl(tmp_path):
    ev = EventLog(tmp_path)
    seen = []
    ev.add_sink(seen.append)
    rec = ev.log("fire", sequence="hotfire_v1")
    assert seen[0]["event"] == "fire"
    files = list(tmp_path.glob("events-*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text().strip())
    assert line["event"] == "fire" and line["time"] == rec["time"]


def test_event_log_tail(tmp_path):
    ev = EventLog(tmp_path)
    for i in range(10):
        ev.log("tick", n=i)
    tail = ev.tail(limit=3)
    assert [e["n"] for e in tail] == [9, 8, 7]  # newest first
    assert ev.tail(limit=100)[-1]["n"] == 0
    # corrupt line is skipped, not fatal
    f = next(tmp_path.glob("events-*.jsonl"))
    f.write_text(f.read_text() + "not json\n")
    assert ev.tail(limit=5)[0]["n"] == 9
