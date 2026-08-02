"""P&ID configs: models, cross-validation, REST save/load, SVG sanitizing."""

import shutil

import httpx
import pytest
from pydantic import ValidationError

from tbctl.config.models import ChannelMap, PidConfig, cross_validate_pid
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


def _chmap() -> ChannelMap:
    import yaml

    from tbctl.config.loader import _YamlLoader

    return ChannelMap.model_validate(
        yaml.load((CONFIGS / "channels.yaml").read_bytes(), Loader=_YamlLoader)
    )


def test_pid_model_validation():
    ok = PidConfig.model_validate(
        {
            "name": "p1",
            "background": "bg.svg",
            "elements": [
                {"id": "v1", "type": "valve", "channel": "valve.ox_main", "x": 0.5, "y": 0.5},
                {"id": "s1", "type": "sensor", "channel": "pressure.chamber", "x": 0.1, "y": 0.1},
                {"id": "t1", "type": "label", "text": "OX", "x": 0, "y": 0},
            ],
        }
    )
    assert ok.elements[0].size == 0.05  # default

    with pytest.raises(ValidationError, match="duplicate element ids"):
        PidConfig.model_validate(
            {
                "name": "p1",
                "elements": [
                    {"id": "a", "type": "label", "text": "x", "x": 0, "y": 0},
                    {"id": "a", "type": "label", "text": "y", "x": 1, "y": 1},
                ],
            }
        )
    with pytest.raises(ValidationError):  # x out of 0-1
        PidConfig.model_validate(
            {
                "name": "p1",
                "elements": [{"id": "a", "type": "label", "text": "x", "x": 1.2, "y": 0}],
            }
        )
    with pytest.raises(ValidationError, match=r"plain .*\.svg"):
        PidConfig.model_validate({"name": "p1", "background": "../evil.svg"})


def test_pid_cross_validation():
    chmap = _chmap()
    pid = PidConfig.model_validate(
        {
            "name": "p1",
            "elements": [
                {"id": "v1", "type": "valve", "channel": "pressure.chamber", "x": 0.5, "y": 0.5},
                {"id": "s1", "type": "sensor", "channel": "valve.ox_main", "x": 0.1, "y": 0.1},
            ],
        }
    )
    errors = cross_validate_pid(pid, chmap)
    assert len(errors) == 2
    assert "not an output channel" in errors[0]
    assert "not an input channel" in errors[1]


async def test_pid_rest_roundtrip(client):
    pids = (await client.get("/api/pid")).json()["pids"]
    assert any(p["name"] == "feed_system" for p in pids)

    body = (await client.get("/api/pid/feed_system")).json()
    cfg = body["config"]
    assert cfg["background"] == "feed_system.svg"
    assert any(e["id"] == "v_ox_main" for e in cfg["elements"])

    # edit + save: move a valve, backup created, bundle reloaded
    for e in cfg["elements"]:
        if e["id"] == "v_ox_main":
            e["x"] = 0.33
    r = await client.post("/api/pid/feed_system", json=cfg)
    assert r.status_code == 200, r.text
    assert r.json()["backup"]
    again = (await client.get("/api/pid/feed_system")).json()["config"]
    assert next(e for e in again["elements"] if e["id"] == "v_ox_main")["x"] == 0.33
    assert len(list((client.cfg / ".backups").glob("feed_system.*.yaml"))) == 1

    # save-as clone under a new name
    assert (await client.post("/api/pid/feed_v2", json=cfg)).status_code == 200
    assert (client.cfg / "pid" / "feed_v2.yaml").is_file()

    # delete
    assert (await client.delete("/api/pid/feed_v2")).status_code == 200
    assert not (client.cfg / "pid" / "feed_v2.yaml").exists()
    assert (await client.get("/api/pid/feed_v2")).status_code == 404


async def test_pid_save_rejects_bad_channel(client):
    cfg = (await client.get("/api/pid/feed_system")).json()["config"]
    cfg["elements"][0]["channel"] = "valve.nope"
    r = await client.post("/api/pid/feed_system", json=cfg)
    assert r.status_code == 422
    assert "not an output channel" in str(r.json())

    cfg = (await client.get("/api/pid/feed_system")).json()["config"]
    cfg["background"] = "missing.svg"
    r = await client.post("/api/pid/feed_system", json=cfg)
    assert r.status_code == 422
    assert "not uploaded" in str(r.json())


async def test_svg_upload_and_sanitizing(client):
    good = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
    r = await client.post("/api/pid/assets", json={"name": "clean", "svg": good})
    assert r.status_code == 200 and r.json()["asset"] == "clean.svg"
    got = await client.get("/api/pid/assets/clean.svg")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/svg+xml")
    assert got.text == good
    assert "clean.svg" in (await client.get("/api/pid/assets")).json()["assets"]

    for evil in (
        "<svg><script>alert(1)</script></svg>",
        "<svg><foreignObject><body/></foreignObject></svg>",
        '<svg onload="alert(1)"><rect/></svg>',
        '<svg><a href="javascript:alert(1)">x</a></svg>',
        "<html>not svg</html>",
    ):
        r = await client.post("/api/pid/assets", json={"name": "evil", "svg": evil})
        assert r.status_code == 422, evil
    assert (await client.get("/api/pid/assets/evil.svg")).status_code == 404
    # traversal names rejected
    r = await client.post("/api/pid/assets", json={"name": "../x", "svg": good})
    assert r.status_code == 400
    assert (await client.get("/api/pid/assets/..%2Fx.svg")).status_code in (400, 404)


async def test_channel_edit_blocked_by_pid_binding(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["channels"] = [c for c in cfg["channels"] if c["tag"] != "pressure.ox_tank"]
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    assert "pid feed_system" in str(r.json())


async def test_dashboard_accepts_pid_panel(client):
    dash = {
        "name": "piddash",
        "grid": {"cols": 12, "row_height_px": 40},
        "panels": [
            {
                "id": "p1",
                "type": "pid",
                "x": 0,
                "y": 0,
                "w": 8,
                "h": 8,
                "channels": [],
                "y_axis": {"mode": "auto", "min": None, "max": None},
                "pid": "feed_system",
            }
        ],
    }
    assert (await client.post("/api/dashboards/piddash", json=dash)).status_code == 200
    got = (await client.get("/api/dashboards/piddash")).json()
    assert got["panels"][0]["pid"] == "feed_system"
