"""Run annotations: CRUD, status workflow, auto-annotation from failed
review rules (replaced on rerun, user annotations untouched)."""

import asyncio
import shutil

import httpx
import pytest

from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS
from tests.test_review import QUICK_SEQ, _fire_quick_sequence


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


async def _quick_run(client, name="ann") -> str:
    await client.post("/api/runs/start", json={"name": name, "operator": "op", "notes": ""})
    await asyncio.sleep(0.5)
    return (await client.post("/api/runs/stop")).json()["run_id"]


async def test_annotation_crud(client):
    run_id = await _quick_run(client)
    t0 = (await client.get(f"/api/runs/{run_id}")).json()["t_start"]

    # create (reversed range gets normalized), list sorted by t0
    r = await client.post(
        f"/api/runs/{run_id}/annotations",
        json={"t0": t0 + 2.0, "t1": t0 + 1.0, "label": "pressure wiggle", "severity": "info"},
    )
    assert r.status_code == 200
    ann = r.json()["annotation"]
    assert ann["t0"] < ann["t1"] and ann["status"] == "open" and ann["source"] == "user"

    r2 = await client.post(
        f"/api/runs/{run_id}/annotations",
        json={"t0": t0, "t1": t0 + 0.5, "label": "startup"},
    )
    anns = (await client.get(f"/api/runs/{run_id}/annotations")).json()["annotations"]
    assert [a["label"] for a in anns] == ["startup", "pressure wiggle"]

    # status workflow + edits
    patched = (
        await client.patch(
            f"/api/runs/{run_id}/annotations/{ann['id']}",
            json={"status": "resolved", "severity": "warn"},
        )
    ).json()["annotation"]
    assert patched["status"] == "resolved" and patched["severity"] == "warn"

    # delete
    aid2 = r2.json()["annotation"]["id"]
    assert (await client.delete(f"/api/runs/{run_id}/annotations/{aid2}")).status_code == 200
    anns = (await client.get(f"/api/runs/{run_id}/annotations")).json()["annotations"]
    assert len(anns) == 1

    # errors
    assert (await client.get("/api/runs/nope/annotations")).status_code == 404
    assert (
        await client.patch(f"/api/runs/{run_id}/annotations/deadbeef", json={"status": "open"})
    ).status_code == 404
    assert (
        await client.post(f"/api/runs/{run_id}/annotations", json={"t0": 0, "t1": 1, "label": "  "})
    ).status_code == 400
    assert (
        await client.post(
            f"/api/runs/{run_id}/annotations",
            json={"t0": 0, "t1": 1, "label": "x", "severity": "fatal"},
        )
    ).status_code == 422


async def test_failed_rules_create_annotations(client):
    client.ctx.iface.sim_set("pressure.chamber", 28.0, noise_sigma_eu=0.0)  # pc_peak fails
    await client.post("/api/runs/start", json={"name": "af", "operator": "op", "notes": ""})
    await asyncio.sleep(0.4)
    await _fire_quick_sequence(client)
    run_id = (await client.post("/api/runs/stop")).json()["run_id"]

    anns = (await client.get(f"/api/runs/{run_id}/annotations")).json()["annotations"]
    rule_anns = [a for a in anns if a["source"] == "rule"]
    assert any("pc_peak" in a["label"] for a in rule_anns)
    assert all(a["severity"] == "crit" for a in rule_anns)

    # add a user annotation, then loosen the rule and rerun: rule annotations
    # are replaced (none failing now), the user one survives
    await client.post(
        f"/api/runs/{run_id}/annotations",
        json={"t0": 0, "t1": 1, "label": "keep me"},
    )
    from tbctl.config.loader import load_review

    review_path = client.ctx.bundle.review.path
    review_path.write_text(
        "rules:\n"
        "  - name: pc_peak\n"
        "    tag: pressure.chamber\n"
        "    metric: max\n"
        "    window: fire\n"
        "    limit_max: 50.0\n"
    )
    client.ctx.bundle.review = load_review(review_path, client.ctx.bundle.channels.model)
    assert (await client.post(f"/api/runs/{run_id}/report/rerun")).status_code == 200
    anns = (await client.get(f"/api/runs/{run_id}/annotations")).json()["annotations"]
    assert [a for a in anns if a["source"] == "rule"] == []
    assert [a["label"] for a in anns] == ["keep me"]


async def test_quick_seq_reusable(client):
    # guard: the shared QUICK_SEQ fixture stays valid against the channel map
    assert (await client.post("/api/sequences/validate", json=QUICK_SEQ)).status_code == 200
