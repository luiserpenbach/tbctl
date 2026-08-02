"""M2 API surface: /api/status, /api/channels, /api/live, WS frames — all on sim."""

import asyncio
import json

import httpx
import msgpack
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS


@pytest.fixture
async def app_ctx(tmp_path):
    # keep run/event files out of the repo tree (was creating ./logs/)
    ctx = build_context(CONFIGS, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    # run lifespan manually so acquisition + ws hub start
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, ctx, client


async def test_status(app_ctx):
    _app, _ctx, client = app_ctx
    r = await client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["device"]["sim"] is True
    assert body["device"]["connected"] is True
    assert body["sequencer"]["state"] == "IDLE"
    assert body["sequencer"]["interp_version"] == 1
    assert body["acquisition"]["running"] is True
    assert body["outputs"]["valve.ox_main"] == "closed"
    assert body["outputs"]["valve.ox_vent"] == "open"  # NC vent fails open
    assert len(body["config"]["channels_sha256"]) == 64


async def test_channels(app_ctx):
    _app, _ctx, client = app_ctx
    r = await client.get("/api/channels")
    body = r.json()
    tags = {c["tag"] for c in body["channels"]}
    assert "pressure.chamber" in tags and "valve.ox_main" in tags
    valve = next(c for c in body["channels"] if c["tag"] == "valve.ox_main")
    assert valve["bit"] == 0
    assert body["rate_groups"]["default"] == 50


async def test_live_endpoint(app_ctx):
    _app, ctx, client = app_ctx
    ctx.iface.sim_set("pressure.chamber", 12.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.4)
    r = await client.get("/api/live", params={"tags": "pressure.chamber", "window_s": 10})
    body = r.json()
    series = body["series"]["pressure.chamber"]
    assert len(series["t"]) >= 5
    assert series["mean"][-1] == pytest.approx(12.0, rel=0.1)
    r404 = await client.get("/api/live", params={"tags": "nope.nope"})
    assert r404.status_code == 404


async def test_sim_set_endpoint(app_ctx):
    _app, _ctx, client = app_ctx
    r = await client.post("/api/sim/set", json={"tag": "pressure.n2_purge", "value": 9.5})
    assert r.status_code == 200
    assert r.json()["setpoints"]["pressure.n2_purge"] == 9.5


async def test_ws_frames(app_ctx):
    """Drive the WS handler directly with a fake websocket."""
    app, _ctx, _client = app_ctx
    hub = app.state.hub

    sent: list[dict] = []

    class FakeWs:
        def __init__(self):
            self._inbox = asyncio.Queue()

        async def accept(self):
            pass

        async def send_bytes(self, b: bytes):
            sent.append(msgpack.unpackb(b))

        async def receive_text(self):
            return await self._inbox.get()

    ws = FakeWs()
    task = asyncio.create_task(hub.handle(ws))
    await ws._inbox.put(json.dumps({"op": "subscribe", "tags": ["pressure.chamber"]}))
    await asyncio.sleep(0.6)
    task.cancel()

    types = {m["type"] for m in sent}
    assert "seq_status" in types
    frames = [m for m in sent if m["type"] == "frame"]
    assert frames, f"no frames in {types}"
    assert frames[-1]["tags"] == ["pressure.chamber"]
    assert len(frames[-1]["data"]) == 1
    assert len(frames[-1]["data"][0]) > 0
    status = next(m for m in sent if m["type"] == "seq_status")
    assert status["state"] == "IDLE"


async def test_ws_frame_timestamps_keep_float64_resolution(app_ctx):
    """Epoch `t` must survive msgpack with sub-second uniqueness (not float32)."""
    app, _ctx, _client = app_ctx
    hub = app.state.hub

    sent: list[dict] = []

    class FakeWs:
        def __init__(self):
            self._inbox = asyncio.Queue()

        async def accept(self):
            pass

        async def send_bytes(self, b: bytes):
            sent.append(msgpack.unpackb(b))

        async def receive_text(self):
            return await self._inbox.get()

    ws = FakeWs()
    task = asyncio.create_task(hub.handle(ws))
    await ws._inbox.put(json.dumps({"op": "subscribe", "tags": ["pressure.chamber"]}))
    await asyncio.sleep(0.8)
    task.cancel()

    frames = [m for m in sent if m["type"] == "frame"]
    assert frames, "expected live frames"
    ts = [t for f in frames for t in f["t"]]
    assert len(ts) >= 5
    assert len(set(ts)) == len(ts), f"collapsed timestamps: {ts[:12]}"
    # Distinct samples at 50 Hz / ~10 Hz emit must differ by well under 1 s.
    assert max(ts) - min(ts) < 2.0
    assert any(ts[i] != ts[i - 1] for i in range(1, len(ts)))
