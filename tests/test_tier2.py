"""Tier 2: run tags/outcome metadata, xy panel type, Python run loader."""

import asyncio
import shutil
import sys
from pathlib import Path

import httpx
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # for tools/


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


async def _quick_run(client, name="t2") -> str:
    await client.post("/api/runs/start", json={"name": name, "operator": "op", "notes": ""})
    await client.post("/api/runs/note", json={"text": "bookmark"})
    await asyncio.sleep(0.6)
    return (await client.post("/api/runs/stop")).json()["run_id"]


async def test_run_meta_tags_and_outcome(client):
    run_id = await _quick_run(client)

    r = await client.patch(
        f"/api/runs/{run_id}/meta",
        json={"tags": ["igniter-test", " campaign-2 ", ""], "outcome": "anomaly"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tags"] == ["igniter-test", "campaign-2"]  # trimmed, empties dropped
    assert body["outcome"] == "anomaly"

    # persisted into list + meta
    runs = (await client.get("/api/runs")).json()["runs"]
    entry = next(x for x in runs if x["run_id"] == run_id)
    assert entry["tags"] == ["igniter-test", "campaign-2"]
    assert entry["outcome"] == "anomaly"

    # partial patch: outcome only, tags untouched; then clear outcome
    await client.patch(f"/api/runs/{run_id}/meta", json={"outcome": "pass"})
    meta = (await client.get(f"/api/runs/{run_id}")).json()
    assert meta["outcome"] == "pass" and meta["tags"] == ["igniter-test", "campaign-2"]
    await client.patch(f"/api/runs/{run_id}/meta", json={"clear_outcome": True})
    assert (await client.get(f"/api/runs/{run_id}")).json()["outcome"] is None

    # validation + unknown run
    assert (
        await client.patch(f"/api/runs/{run_id}/meta", json={"outcome": "meh"})
    ).status_code == 422
    assert (await client.patch("/api/runs/nope/meta", json={"tags": []})).status_code == 404


async def test_xy_panel_type_accepted(client):
    dash = {
        "name": "xydash",
        "grid": {"cols": 12, "row_height_px": 40},
        "panels": [
            {
                "id": "p1",
                "type": "xy",
                "x": 0,
                "y": 0,
                "w": 5,
                "h": 6,
                "channels": ["pressure.chamber", "force.thrust"],
                "y_axis": {"mode": "auto", "min": None, "max": None},
            }
        ],
    }
    assert (await client.post("/api/dashboards/xydash", json=dash)).status_code == 200
    got = (await client.get("/api/dashboards/xydash")).json()
    assert got["panels"][0]["channels"] == ["pressure.chamber", "force.thrust"]


async def test_python_loader(client):
    run_id = await _quick_run(client)
    await client.patch(f"/api/runs/{run_id}/meta", json={"tags": ["x"], "outcome": "pass"})
    await client.post(f"/api/runs/{run_id}/annotations", json={"t0": 0, "t1": 1, "label": "band"})
    path = client.ctx.recorder.run_path(run_id)

    from tools.tbctl_load import load_run

    run = load_run(path)
    assert run.meta["run_id"] == run_id
    assert run.meta["tags"] == ["x"] and run.meta["outcome"] == "pass"
    t, pc = run.channel("pressure.chamber")
    assert len(t) == len(pc) > 10
    assert run.channel("calc.dp_ox_inj")[1].shape == t.shape  # derived recorded too
    assert any(e["event"] == "note" for e in run.events)
    assert run.report is not None  # review rules ran at stop
    assert [a["label"] for a in run.annotations if a["source"] == "user"] == ["band"]
    with pytest.raises(KeyError):
        run.channel("nope.tag")
