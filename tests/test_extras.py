"""Phase 15 extras: run notes, run events, CSV export, checklists."""

import asyncio
import shutil

import httpx
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS


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


async def _record_briefly(client, name="t") -> str:
    r = await client.post("/api/runs/start", json={"name": name, "operator": "op", "notes": ""})
    assert r.status_code == 200
    run_id = r.json()["run_id"]
    await asyncio.sleep(0.8)  # let some samples + a flush land
    return run_id


async def test_run_notes_and_events(client):
    # no active run -> 409
    assert (await client.post("/api/runs/note", json={"text": "x"})).status_code == 409

    run_id = await _record_briefly(client)
    assert (await client.post("/api/runs/note", json={"text": "  "})).status_code == 400
    assert (
        await client.post("/api/runs/note", json={"text": "MV open, good spray"})
    ).status_code == 200
    await asyncio.sleep(0.7)  # flush interval
    await client.post("/api/runs/stop")

    events = (await client.get(f"/api/runs/{run_id}/events")).json()["events"]
    notes = [e for e in events if e["event"] == "note"]
    assert len(notes) == 1
    assert notes[0]["text"] == "MV open, good spray"
    assert notes[0]["run_id"] == run_id
    assert notes[0]["time"] > 0
    assert (await client.get("/api/runs/nope/events")).status_code == 404


async def test_csv_export(client):
    run_id = await _record_briefly(client)
    await client.post("/api/runs/stop")

    r = await client.get(f"/api/runs/{run_id}/export.csv?tags=pressure.chamber,temp.injector")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.strip().split("\n")
    assert lines[0] == "t_epoch,pressure.chamber,temp.injector"
    assert len(lines) > 10
    # both same rate group here: every row fully populated, t ascending
    first = lines[1].split(",")
    assert len(first) == 3 and float(first[0]) > 0 and first[1] != ""
    ts = [float(ln.split(",")[0]) for ln in lines[1:]]
    assert ts == sorted(ts)

    # t0/t1 window narrows the export
    mid = ts[len(ts) // 2]
    r2 = await client.get(f"/api/runs/{run_id}/export.csv?tags=pressure.chamber&t0={mid}")
    assert 0 < len(r2.text.strip().split("\n")) - 1 < len(ts)

    assert (await client.get(f"/api/runs/{run_id}/export.csv?tags=nope.tag")).status_code == 404
    assert (await client.get(f"/api/runs/{run_id}/export.csv?tags=")).status_code == 400


async def test_checklists(client):
    lists = (await client.get("/api/checklists")).json()
    assert any(c["name"] == "pre_fire" for c in lists["checklists"])
    assert lists["active"] is None

    # activate + check items; progress appears in /api/status
    r = await client.post("/api/checklists/activate", json={"name": "pre_fire"})
    assert r.status_code == 200
    n = len(r.json()["active"]["items"])
    assert (
        await client.post("/api/checklists/check", json={"index": 0, "checked": True})
    ).status_code == 200
    await client.post("/api/checklists/check", json={"index": 1, "checked": True})
    st = (await client.get("/api/status")).json()["checklist"]
    assert st == {"name": "pre_fire", "total": n, "checked": 2}

    # uncheck, bad index, unknown list
    await client.post("/api/checklists/check", json={"index": 0, "checked": False})
    assert (await client.get("/api/status")).json()["checklist"]["checked"] == 1
    assert (
        await client.post("/api/checklists/check", json={"index": 99, "checked": True})
    ).status_code == 400
    assert (await client.post("/api/checklists/activate", json={"name": "nope"})).status_code == 404

    # events logged
    events = (await client.get("/api/events?limit=50")).json()["events"]
    assert any(e["event"] == "checklist_item" for e in events)

    # deactivate
    await client.post("/api/checklists/activate", json={"name": None})
    assert (await client.get("/api/status")).json()["checklist"] is None
    assert (
        await client.post("/api/checklists/check", json={"index": 0, "checked": True})
    ).status_code == 409


async def test_checklist_panel_type_accepted(client):
    dash = {
        "name": "chk",
        "grid": {"cols": 12, "row_height_px": 40},
        "panels": [
            {
                "id": "p1",
                "type": "checklist_panel",
                "x": 0,
                "y": 0,
                "w": 4,
                "h": 6,
                "channels": [],
                "y_axis": {"mode": "auto", "min": None, "max": None},
            }
        ],
    }
    assert (await client.post("/api/dashboards/chk", json=dash)).status_code == 200
