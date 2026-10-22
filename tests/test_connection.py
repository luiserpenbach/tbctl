"""Connection manager: info, guarded switching, failure restore, register peek."""

import asyncio

import httpx
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS


@pytest.fixture
async def client(tmp_path):
    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.ctx = ctx
            yield c


async def test_connection_info(client):
    info = (await client.get("/api/connection")).json()
    assert info["mode"] == "sim"
    assert info["sim"] is True and info["connected"] is True
    assert info["device"]["serial"] == 470000001
    assert info["interpreter"]["ok"] is True
    assert info["stream"]["running"] is True


async def test_switch_to_usb_fails_and_restores_sim(client, monkeypatch):
    """A failed USB open must restore the simulator. The open is forced to
    fail so a host with LJM and a free T7 attached never touches it."""
    from tbctl.hw.ljm_iface import DeviceError, LjmDevice

    def no_device(self):
        raise DeviceError("LJME_DEVICE_NOT_FOUND")

    monkeypatch.setattr(LjmDevice, "open", no_device)
    r = await client.post("/api/connection", json={"mode": "usb"})
    assert r.status_code == 502
    assert "previous connection restored" in r.json()["detail"]
    info = (await client.get("/api/connection")).json()
    assert info["mode"] == "sim" and info["connected"] is True
    assert "switch to usb failed" in (info["last_error"] or "")
    # stream restarted and flowing again
    await asyncio.sleep(0.4)
    info2 = (await client.get("/api/connection")).json()
    assert info2["stream"]["running"] is True
    assert info2["stream"]["sample_count"] > 0


async def test_sim_to_sim_switch_works(client):
    before = (await client.get("/api/connection")).json()
    r = await client.post("/api/connection", json={"mode": "sim"})
    assert r.status_code == 200
    after = r.json()
    assert after["mode"] == "sim" and after["connected"] is True
    assert after["switched_at"] >= before["switched_at"]
    await asyncio.sleep(0.3)
    assert (await client.get("/api/status")).json()["acquisition"]["running"] is True


async def test_switch_guards(client):
    # control mode on -> refuse
    await client.post("/api/control-mode", json={"enabled": True})
    r = await client.post("/api/connection", json={"mode": "sim"})
    assert r.status_code == 409 and "control mode" in r.json()["detail"]
    await client.post("/api/control-mode", json={"enabled": False})
    # recording -> refuse
    await client.post("/api/runs/start", json={"name": "x", "operator": "t"})
    r = await client.post("/api/connection", json={"mode": "sim"})
    assert r.status_code == 409 and "recording" in r.json()["detail"]
    await client.post("/api/runs/stop")


async def test_deploy_rejected_in_sim(client):
    r = await client.post("/api/connection/deploy-lua")
    assert r.status_code == 409


async def test_register_peek(client):
    r = await client.get("/api/connection/read", params={"name": "TEMPERATURE_DEVICE_K"})
    assert r.json()["value"] == pytest.approx(298.15)
    assert (
        await client.get("/api/connection/read", params={"name": "bad name!"})
    ).status_code == 400


async def test_sim_set_still_works_after_switch(client):
    await client.post("/api/connection", json={"mode": "sim"})
    r = await client.post("/api/sim/set", json={"tag": "pressure.chamber", "value": 7.5})
    assert r.status_code == 200


async def test_raw_endpoint(client):
    ctx = client.ctx
    ctx.iface.sim_set("pressure.chamber", 10.0, noise_sigma_eu=0.0)  # 25 bar/V -> 0.4 V
    await asyncio.sleep(0.4)
    rows = (await client.get("/api/raw")).json()["channels"]
    chamber = next(r for r in rows if r["tag"] == "pressure.chamber")
    assert chamber["hw"] == "AIN0" and chamber["rate_hz"] == 50
    assert chamber["raw"] == pytest.approx(0.4, abs=0.02)
    assert chamber["value"] == pytest.approx(10.0, rel=0.05)
    assert len(rows) == len([c for c in ctx.bundle.channels.model.inputs()])


async def test_sim_setpoints_endpoint(client):
    await client.post("/api/sim/set", json={"tag": "pressure.ox_tank", "value": 42.0})
    sp = (await client.get("/api/sim/setpoints")).json()["setpoints"]
    assert sp["pressure.ox_tank"] == 42.0
