"""Simulator chamber model (configs/sim.yaml): ignition logic, first-order
Pc response, coupled channels, config validation and the end-to-end path."""

import asyncio
import random
import shutil

import httpx
import numpy as np
import pytest

from tbctl.config.loader import ConfigError, load_channels, load_config_dir, load_sim
from tbctl.config.models import SimConfig
from tbctl.hw.ljm_mock import ChamberSim, MockDevice
from tbctl.node import build_app, build_context
from tests.conftest import CONFIGS


@pytest.fixture
def chmap():
    return load_channels(CONFIGS / "channels.yaml").model


@pytest.fixture
def plant(chmap):
    return load_sim(CONFIGS / "sim.yaml", chmap).model


def _word(chmap, **open_tags) -> int:
    """DIO word with the safe image, then the given outputs active (open/on)."""
    word = chmap.safe_word()
    for tag, on in open_tags.items():
        c = chmap.by_tag(tag.replace("__", "."))
        bit, level = chmap.bit_index(c.tag), chmap.active_level(c)
        word = (word & ~(1 << bit)) | ((level if on else 1 - level) << bit)
    return word


def _run(sim: ChamberSim, word: int, ms: int, t0: float = 0.0) -> float:
    for k in range(ms):
        sim.step(t0 + k / 1000.0, word)
    return t0 + ms / 1000.0


def test_ignites_after_delay_and_settles(chmap, plant):
    sim = ChamberSim(plant.chamber, chmap)
    lit_word = _word(chmap, valve__ox_main=True, valve__fuel_main=True, igniter__main=True)
    t = _run(sim, lit_word, 79)
    assert not sim.lit and sim.pc == pytest.approx(1.0, rel=0.2)  # cold flow until lit
    t = _run(sim, lit_word, 2, t)
    assert sim.lit
    # igniter off again: stays lit while both propellants flow
    _run(sim, _word(chmap, valve__ox_main=True, valve__fuel_main=True), 500, t)
    assert sim.lit and sim.pc == pytest.approx(15.0, rel=1e-3)


def test_no_igniter_means_cold_flow_only(chmap, plant):
    sim = ChamberSim(plant.chamber, chmap)
    _run(sim, _word(chmap, valve__ox_main=True, valve__fuel_main=True), 1000)
    assert not sim.lit and sim.pc == pytest.approx(1.0, rel=1e-3)


def test_closing_a_valve_puts_the_flame_out(chmap, plant):
    sim = ChamberSim(plant.chamber, chmap)
    t = _run(sim, _word(chmap, valve__ox_main=True, valve__fuel_main=True, igniter__main=True), 500)
    t = _run(sim, _word(chmap, valve__ox_main=True), 500, t)  # fuel shut, ox still flowing
    assert not sim.lit and sim.pc == pytest.approx(1.0, rel=0.01)
    _run(sim, _word(chmap), 1000, t)  # all closed: decays to rest and settles exactly
    assert sim.pc == 0.0


def test_coupled_channels(chmap, plant):
    # roughness off, so the offsets are exact
    quiet = SimConfig.model_validate({"chamber": {**plant.chamber.model_dump(), "roughness": 0}})
    sim = ChamberSim(quiet.chamber, chmap)
    word = _word(chmap, valve__ox_main=True, valve__fuel_main=True, igniter__main=True)
    t = _run(sim, word, 600)
    rng = random.Random(0)
    assert sim.eu_offset("pressure.chamber", t, word, rng) == pytest.approx(15.0, rel=1e-3)
    assert sim.eu_offset("force.thrust", t, word, rng) == pytest.approx(300.0, rel=1e-3)
    assert sim.eu_offset("pressure.ox_manifold", t, word, rng) == pytest.approx(18.75, rel=1e-3)
    assert sim.eu_offset("pressure.ox_tank", t, word, rng) == 0.0


def test_sim_yaml_cross_validation(chmap, tmp_path):
    p = tmp_path / "sim.yaml"
    p.write_text(
        "chamber:\n  tag: pressure.nope\n  ox_valve: igniter.main\n"
        "  fuel_valve: valve.fuel_main\n  pc_bar: 10\n"
    )
    with pytest.raises(ConfigError) as e:
        load_sim(p, chmap)
    assert "pressure.nope" in str(e.value) and "igniter.main" in str(e.value)


def test_mock_disables_plant_on_mismatched_channels(chmap, plant):
    bad = SimConfig.model_validate(
        {"chamber": {**plant.chamber.model_dump(), "tag": "pressure.renamed"}}
    )
    assert MockDevice(chmap, plant=bad)._chamber is None
    assert MockDevice(chmap, plant=plant)._chamber is not None
    assert load_config_dir(CONFIGS).sim is not None


async def test_hot_fire_end_to_end(tmp_path):
    cfg = tmp_path / "configs"
    shutil.copytree(CONFIGS, cfg)
    ctx = build_context(cfg, sim=True, data_dir=tmp_path / "runs", log_dir=tmp_path / "logs")
    app = build_app(ctx)
    seq = {
        "name": "short_fire",
        "test_type": "engine",
        "max_duration_s": 2.0,
        "steps": [
            {"t": 0.0, "set": {"igniter.main": "on"}},
            {"t": 0.05, "set": {"valve.ox_main": "open", "valve.fuel_main": "open"}},
            {"t": 0.25, "set": {"igniter.main": "off"}},
            {"t": 1.0, "set": {"valve.ox_main": "close", "valve.fuel_main": "close"}},
        ],
        "abort": [{"t": 0.0, "set": {"valve.ox_main": "close", "valve.fuel_main": "close"}}],
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            await c.post("/api/runs/start", json={"name": "hf", "operator": "op", "notes": ""})
            assert (await c.post("/api/sequences/short_fire", json=seq)).status_code == 200
            await c.post("/api/control-mode", json={"enabled": True})
            assert (await c.post("/api/sequences/short_fire/load")).status_code == 200
            for op in ("arm", "fire"):
                r = await c.post(f"/api/sequences/{op}", json={"confirm": "short_fire"})
                assert r.status_code == 200
            await asyncio.sleep(1.6)
            await c.post("/api/sequences/reset")
            run_id = (await c.post("/api/runs/stop")).json()["run_id"]
            data = ctx.recorder.run_data(
                run_id, ["pressure.chamber", "force.thrust"], max_points=100_000
            )
    pc = np.array(data["pressure.chamber"]["mean"])
    thrust = np.array(data["force.thrust"]["mean"])
    assert 13.0 < np.median(pc[pc > 5]) < 17.0  # lit plateau near pc_bar
    assert pc[-1] < 1.0  # shut down and decayed
    assert np.max(thrust) > 250.0  # follows Pc at 20 N/bar
