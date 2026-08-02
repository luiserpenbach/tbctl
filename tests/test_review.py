"""Post-run review rules: model validation, fire-window evaluation,
auto-report on run stop, rerun endpoint, sequence scoping."""

import asyncio
import shutil

import httpx
import pytest
from pydantic import ValidationError

from tbctl.config.models import ReviewConfig
from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS

QUICK_SEQ = {
    "name": "quick_test",
    "description": "fast sim sequence for review tests",
    "max_duration_s": 1.0,
    "preconditions": [],
    "steps": [
        {"t": 0.0, "set": {"valve.purge": "open"}},
        {"t": 0.1, "set": {"valve.purge": "close"}},
    ],
    "abort": [{"t": 0.0, "set": {"valve.purge": "close"}}],
    "allow_unsafe_end": False,
    "redlines": [],
}


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


def test_review_model_validation():
    with pytest.raises(ValidationError, match="limit_min and/or limit_max"):
        ReviewConfig.model_validate(
            {"rules": [{"name": "r", "tag": "pressure.chamber", "metric": "max"}]}
        )
    with pytest.raises(ValidationError, match="duplicate rule names"):
        ReviewConfig.model_validate(
            {
                "rules": [
                    {"name": "r", "tag": "a.b", "metric": "max", "limit_max": 1},
                    {"name": "r", "tag": "a.c", "metric": "min", "limit_min": 0},
                ]
            }
        )
    with pytest.raises(ValidationError):
        ReviewConfig.model_validate(
            {"rules": [{"name": "r", "tag": "a.b", "metric": "median", "limit_max": 1}]}
        )


async def _fire_quick_sequence(client):
    assert (await client.post("/api/sequences/quick_test", json=QUICK_SEQ)).status_code == 200
    await client.post("/api/control-mode", json={"enabled": True})
    assert (await client.post("/api/sequences/quick_test/load")).status_code == 200
    assert (
        await client.post("/api/sequences/arm", json={"confirm": "quick_test"})
    ).status_code == 200
    assert (
        await client.post("/api/sequences/fire", json={"confirm": "quick_test"})
    ).status_code == 200
    # wait for DONE
    for _ in range(50):
        st = (await client.get("/api/status")).json()["sequencer"]["state"]
        if st == "DONE":
            break
        await asyncio.sleep(0.1)
    assert st == "DONE"
    await client.post("/api/sequences/reset")
    await client.post("/api/control-mode", json={"enabled": False})


async def test_report_generated_on_stop(client):
    client.ctx.iface.sim_set("pressure.chamber", 15.0, noise_sigma_eu=0.0)
    client.ctx.iface.sim_set("pressure.ox_manifold", 20.0, noise_sigma_eu=0.0)  # dp_ox ~ +5
    client.ctx.iface.sim_set("pressure.ox_tank", 30.0, noise_sigma_eu=0.0)
    await client.post("/api/runs/start", json={"name": "rv", "operator": "op", "notes": ""})
    await asyncio.sleep(0.4)
    await _fire_quick_sequence(client)
    await asyncio.sleep(0.4)
    run_id = (await client.post("/api/runs/stop")).json()["run_id"]

    report = (await client.get(f"/api/runs/{run_id}/report")).json()["report"]
    assert report is not None
    assert report["sequence"] == "quick_test"
    assert report["fire_window"] is not None
    by_name = {r["name"]: r for r in report["results"]}
    # pc_peak: fire-window max of ~15 bar, limit 25 -> pass
    assert by_name["pc_peak"]["passed"] is True
    assert by_name["pc_peak"]["value"] == pytest.approx(15.0, abs=1.0)
    # ox_tank_sane: min over run of ~30 -> pass
    assert by_name["ox_tank_sane"]["passed"] is True
    # wall_temp_max is scoped to hotfire_v1 -> not in this report
    assert "wall_temp_max" not in by_name
    s = report["summary"]
    assert s["failed"] == 0 and s["passed"] >= 3

    # summary surfaces in run metadata + report event logged
    meta = (await client.get(f"/api/runs/{run_id}")).json()
    assert meta["report_summary"]["failed"] == 0
    events = (await client.get(f"/api/runs/{run_id}/events")).json()["events"]
    assert not any(e["event"] == "run_report" for e in events)  # logged after close


async def test_report_failing_rule_and_rerun(client):
    client.ctx.iface.sim_set("pressure.chamber", 28.0, noise_sigma_eu=0.0)  # over pc limit
    await client.post("/api/runs/start", json={"name": "fail", "operator": "op", "notes": ""})
    await asyncio.sleep(0.4)
    await _fire_quick_sequence(client)
    run_id = (await client.post("/api/runs/stop")).json()["run_id"]

    report = (await client.get(f"/api/runs/{run_id}/report")).json()["report"]
    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["pc_peak"]["passed"] is False
    assert report["summary"]["failed"] >= 1

    # loosen the rule in the bundle and rerun -> now passes
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
    report2 = (await client.post(f"/api/runs/{run_id}/report/rerun")).json()["report"]
    assert {r["name"]: r for r in report2["results"]}["pc_peak"]["passed"] is True
    # persisted
    again = (await client.get(f"/api/runs/{run_id}/report")).json()["report"]
    assert again["summary"]["failed"] == 0


async def test_report_without_fire_skips_fire_rules(client):
    await client.post("/api/runs/start", json={"name": "nofire", "operator": "op", "notes": ""})
    await asyncio.sleep(0.5)
    run_id = (await client.post("/api/runs/stop")).json()["run_id"]
    report = (await client.get(f"/api/runs/{run_id}/report")).json()["report"]
    by_name = {r["name"]: r for r in report["results"]}
    assert by_name["pc_peak"]["skipped"] == "run never fired a sequence"
    assert by_name["pc_peak"]["passed"] is None
    assert by_name["ox_tank_sane"]["passed"] is not None  # run-window rule still runs

    assert (await client.get("/api/runs/nope/report")).status_code == 404
