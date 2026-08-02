"""Sequence editor: validate (dry-run compile + timeline), save with backup,
save-as cloning, and the loaded/armed edit guard."""

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


async def test_get_sequence_config(client):
    body = (await client.get("/api/sequences/coldflow_v1/config")).json()
    assert body["config"]["name"] == "coldflow_v1"
    assert body["config"]["steps"][0] == {"t": 0.0, "set": {"valve.purge": "open"}}
    assert body["editable"] is True
    assert len(body["sha256"]) == 64
    assert (await client.get("/api/sequences/nope/config")).status_code == 404


async def test_validate_dry_run(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    r = await client.post("/api/sequences/validate", json=cfg)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["report"]["n_steps"] == 6
    tl = body["timeline"]
    assert tl["duration_ms"] == 8200
    assert [o["tag"] for o in tl["outputs"]] == [
        c["tag"]
        for c in (await client.get("/api/channels")).json()["channels"]
        if c["kind"] in ("valve", "spark")
    ]
    # carry-forward words: purge-open step keeps everything else at safe
    purge_bit = next(o["bit"] for o in tl["outputs"] if o["tag"] == "valve.purge")
    assert tl["steps"][0]["t_ms"] == 0
    assert (tl["steps"][0]["word"] >> purge_bit) & 1 == 1
    assert tl["steps"][1]["word"] == tl["safe_word"]  # purge closed again
    # nothing was written
    assert not (client.cfg / ".backups").exists()


async def test_validate_compile_errors(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    cfg["steps"][-1] = {"t": 8.2, "set": {"valve.ox_main": "open"}}  # unsafe end
    r = await client.post("/api/sequences/validate", json=cfg)
    assert r.status_code == 422
    assert "safe_word" in str(r.json())

    cfg["steps"][-1] = {"t": 8.2, "set": {"pressure.chamber": "open"}}  # not an output
    r = await client.post("/api/sequences/validate", json=cfg)
    assert r.status_code == 422
    assert "not an output channel" in str(r.json())


async def test_validate_model_errors(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    cfg["max_duration_s"] = -1
    r = await client.post("/api/sequences/validate", json=cfg)
    assert r.status_code == 422
    locs = [e["loc"] for e in r.json()["detail"]]
    assert any("max_duration_s" in loc for loc in locs)


async def test_save_with_backup_and_reload(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    cfg["description"] = "edited in test"
    cfg["steps"][3]["t"] = 4.5  # shorten the flow
    r = await client.post("/api/sequences/coldflow_v1", json=cfg)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and r.json()["backup"]

    # bundle hot-reloaded: list + config reflect the edit, and it still loads
    seqs = (await client.get("/api/sequences")).json()["sequences"]
    entry = next(s for s in seqs if s["name"] == "coldflow_v1")
    assert entry["description"] == "edited in test"
    assert len(list((client.cfg / ".backups").glob("coldflow_v1.*.yaml"))) == 1
    assert "edited in test" in (client.cfg / "sequences" / "coldflow_v1.yaml").read_text()
    assert (await client.post("/api/sequences/coldflow_v1/load")).status_code == 200


async def test_save_as_clone(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    r = await client.post("/api/sequences/coldflow_v2", json=cfg)
    assert r.status_code == 200, r.text
    names = [s["name"] for s in (await client.get("/api/sequences")).json()["sequences"]]
    assert "coldflow_v2" in names and "coldflow_v1" in names
    assert (client.cfg / "sequences" / "coldflow_v2.yaml").is_file()
    # clone saved under the URL name even though the body said coldflow_v1
    body = (await client.get("/api/sequences/coldflow_v2/config")).json()
    assert body["config"]["name"] == "coldflow_v2"


async def test_invalid_save_writes_nothing(client):
    cfg = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    before = (client.cfg / "sequences" / "coldflow_v1.yaml").read_bytes()
    cfg["steps"][-1] = {"t": 8.2, "set": {"valve.ox_main": "open"}}
    assert (await client.post("/api/sequences/coldflow_v1", json=cfg)).status_code == 422
    assert (client.cfg / "sequences" / "coldflow_v1.yaml").read_bytes() == before
    assert not (client.cfg / ".backups").exists()
    assert (await client.post("/api/sequences/bad name!", json=cfg)).status_code in (400, 404, 422)
    assert (await client.post("/api/sequences/validate99/x", json=cfg)).status_code in (404, 405)


async def test_loaded_sequence_is_read_only(client):
    assert (await client.post("/api/sequences/coldflow_v1/load")).status_code == 200
    body = (await client.get("/api/sequences/coldflow_v1/config")).json()
    assert body["editable"] is False
    cfg = body["config"]
    r = await client.post("/api/sequences/coldflow_v1", json=cfg)
    assert r.status_code == 409
    # a different sequence is still editable
    other = (await client.get("/api/sequences/hotfire_v1/config")).json()
    assert other["editable"] is True
    # after reset the device is IDLE again -> editable
    await client.post("/api/sequences/reset")
    assert (await client.get("/api/sequences/coldflow_v1/config")).json()["editable"] is True
    assert (await client.post("/api/sequences/coldflow_v1", json=cfg)).status_code == 200
