"""M4 API surface: run lifecycle + historical data + dashboard save."""

import asyncio

import httpx
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS


@pytest.fixture
async def client(tmp_path, monkeypatch):
    # isolate configs so dashboard saves don't touch the repo
    import shutil

    cfg = tmp_path / "configs"
    shutil.copytree(CONFIGS, cfg)
    ctx = build_context(cfg, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.ctx = ctx
            yield c


async def test_run_lifecycle_via_api(client):
    r = await client.post(
        "/api/runs/start", json={"name": "api test", "operator": "luis", "notes": "n"}
    )
    assert r.status_code == 200
    run_id = r.json()["run_id"]

    status = (await client.get("/api/status")).json()
    assert status["recording"]["active"] is True
    assert status["recording"]["run_id"] == run_id

    # double-start rejected
    assert (
        await client.post("/api/runs/start", json={"name": "x", "operator": "y"})
    ).status_code == 409

    await asyncio.sleep(0.8)
    r = await client.post("/api/runs/stop")
    assert r.status_code == 200
    assert r.json()["sample_counts"]["default"] > 20

    runs = (await client.get("/api/runs")).json()["runs"]
    assert runs[0]["run_id"] == run_id

    meta = (await client.get(f"/api/runs/{run_id}")).json()
    assert meta["operator"] == "luis"

    data = (
        await client.get(
            f"/api/runs/{run_id}/data", params={"tags": "pressure.chamber", "max_points": 100}
        )
    ).json()
    assert len(data["series"]["pressure.chamber"]["t"]) > 0

    assert (await client.get("/api/runs/nope/data", params={"tags": "a.b"})).status_code == 404
    assert (await client.post("/api/runs/stop")).status_code == 409


async def test_dashboard_save(client):
    dash = (await client.get("/api/dashboards/hotfire_main")).json()
    dash["panels"][0]["w"] = 6
    r = await client.post("/api/dashboards/hotfire_main", json=dash)
    assert r.status_code == 200
    again = (await client.get("/api/dashboards/hotfire_main")).json()
    assert again["panels"][0]["w"] == 6

    dash["panels"][0]["x"] = 99  # overflows grid -> 422
    assert (await client.post("/api/dashboards/hotfire_main", json=dash)).status_code == 422
