"""Channel config editor: validated writes, backups, hot-apply, guards."""

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
            c.cfg = cfg
            yield c


async def test_get_config(client):
    body = (await client.get("/api/config/channels")).json()
    assert body["config"]["device"]["ip"] == "192.168.10.20"
    assert any(c["tag"] == "pressure.chamber" for c in body["config"]["channels"])
    assert len(body["sha256"]) == 64


async def test_edit_cal_and_hot_apply(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    for c in cfg["channels"]:
        if c["tag"] == "pressure.chamber":
            c["cal"] = {"kind": "linear", "gain": 50.0, "offset": -1.0}
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["backup"]

    # hot-applied: sim regenerates and the new cal is live (0 V wobble -> ~-1)
    client.ctx.iface.sim_set("pressure.chamber", 24.0, noise_sigma_eu=0.0)  # 24 EU -> 0.5 V
    await asyncio.sleep(0.5)
    rows = (await client.get("/api/raw")).json()["channels"]
    chamber = next(c for c in rows if c["tag"] == "pressure.chamber")
    assert chamber["raw"] == pytest.approx(0.5, abs=0.03)
    assert chamber["value"] == pytest.approx(24.0, rel=0.1)

    # backup exists, YAML on disk updated
    backups = list((client.cfg / ".backups").glob("channels.*.yaml"))
    assert len(backups) == 1
    assert "gain: 50.0" in (client.cfg / "channels.yaml").read_text()


async def test_add_and_remove_channel(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["channels"].append(
        {"tag": "pressure.new_pt", "hw": "AIN8", "kind": "analog_in", "unit": "bar"}
    )
    assert (await client.post("/api/config/channels", json=cfg)).status_code == 200
    tags = [c["tag"] for c in (await client.get("/api/channels")).json()["channels"]]
    assert "pressure.new_pt" in tags
    await asyncio.sleep(0.3)
    raw = (await client.get("/api/raw")).json()["channels"]
    assert any(c["tag"] == "pressure.new_pt" for c in raw)


async def test_invalid_config_rejected(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["channels"][0]["hw"] = cfg["channels"][1]["hw"]  # duplicate pin
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    assert "duplicate hw" in str(r.json())
    # nothing was written
    assert (
        not list((client.cfg / ".backups").glob("*"))
        if (client.cfg / ".backups").exists()
        else True
    )


async def test_removing_sequenced_channel_rejected(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["channels"] = [c for c in cfg["channels"] if c["tag"] != "valve.ox_main"]
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    assert "valve.ox_main" in str(r.json()) and "sequence" in str(r.json())


async def test_guards_block_edit(client):
    await client.post("/api/control-mode", json={"enabled": True})
    cfg = (await client.get("/api/config/channels")).json()["config"]
    assert (await client.post("/api/config/channels", json=cfg)).status_code == 409
    await client.post("/api/control-mode", json={"enabled": False})
