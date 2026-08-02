"""Calculated channels: expression safety, model validation, acquisition
integration (same-group and cross-group refs), recorder + API flow."""

import asyncio
import shutil

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from tbctl.config.expr import ExprError, compile_expr, expr_refs
from tbctl.config.loader import load_channels
from tbctl.config.models import ChannelMap
from tbctl.hw.ljm_mock import MockDevice
from tbctl.node import build_app, build_context
from tbctl.services.acquisition import AcquisitionService
from tests.conftest import CONFIGS

# ---------------------------------------------------------------- expressions


def test_expr_arithmetic_and_funcs():
    fn, refs = compile_expr("pressure.ox_manifold - pressure.chamber")
    assert refs == ["pressure.ox_manifold", "pressure.chamber"]
    out = fn({"pressure.ox_manifold": np.array([5.0, 6.0]), "pressure.chamber": 2.0})
    assert out.tolist() == [3.0, 4.0]

    fn, _ = compile_expr("sqrt(abs(a.x)) * 2 + max(a.x, 0)")
    assert fn({"a.x": np.array([-4.0])}).tolist() == [4.0]
    fn, _ = compile_expr("(a.x + 1) ** 2 / 2")
    assert fn({"a.x": 1.0}) == 2.0
    fn, _ = compile_expr("-a.x + +a.x * 3")
    assert fn({"a.x": 2.0}) == 4.0


def test_expr_rejects_unsafe():
    for bad in (
        "__import__('os')",
        "a.x if 1 else 2",
        "a.x > 3",
        "lambda: 1",
        "a.x[0]",
        "unknownfn(a.x)",
        "min(a.x)",  # wrong arity
        "abs(a.x, 1)",
        "'str'",
        "a.x; b.y",
        "3 + 4",  # no channel refs
        "abs",  # bare function
        "f'{a.x}'",
    ):
        with pytest.raises(ExprError):
            compile_expr(bad)


def test_expr_refs_dedupe():
    assert expr_refs("a.x + a.x * a.y") == ["a.x", "a.y"]


def test_expr_rejects_bool_constants():
    """L16: bool is an int subclass — `a.x + True` must not evaluate."""
    for bad in ("a.x + True", "False * a.x", "max(a.x, True)"):
        with pytest.raises(ExprError, match="numeric constants"):
            compile_expr(bad)


def test_expr_syntax_error_mentions_tag_grammar():
    """L16: legal tags like temp.2nd can never appear in an expression; the
    error must say why instead of a bare 'invalid syntax'."""
    with pytest.raises(ExprError, match="dotted Python names"):
        compile_expr("temp.2nd + 1")


# --------------------------------------------------------------------- model


def _base_channels():
    return [
        {
            "tag": "pressure.a",
            "hw": "AIN0",
            "kind": "analog_in",
            "unit": "bar",
            "cal": {"kind": "linear", "gain": 10.0, "offset": 0.0},
        },
        {
            "tag": "pressure.b",
            "hw": "AIN1",
            "kind": "analog_in",
            "unit": "bar",
            "cal": {"kind": "linear", "gain": 10.0, "offset": 0.0},
        },
    ]


def _chmap(extra: list[dict]) -> ChannelMap:
    return ChannelMap.model_validate(
        {
            "device": {"ip": "192.168.10.20", "watchdog_timeout_s": 5},
            "rate_groups": {"default": {"rate_hz": 50}},
            "channels": _base_channels() + extra,
        }
    )


def test_derived_model_validation():
    m = _chmap(
        [{"tag": "calc.d", "kind": "derived", "unit": "bar", "expr": "pressure.a - pressure.b"}]
    )
    assert [d.tag for d in m.derived()] == ["calc.d"]
    assert m.derived_group(m.derived()[0]) == "default"
    assert "calc.d" in m.readable_tags()

    with pytest.raises(ValidationError, match="not a physical input"):
        _chmap([{"tag": "calc.d", "kind": "derived", "expr": "pressure.nope + pressure.a"}])
    with pytest.raises(ValidationError, match="not a physical input"):  # no chaining
        _chmap(
            [
                {"tag": "calc.d1", "kind": "derived", "expr": "pressure.a * 2"},
                {"tag": "calc.d2", "kind": "derived", "expr": "calc.d1 * 2"},
            ]
        )
    with pytest.raises(ValidationError, match="bad expression"):
        _chmap([{"tag": "calc.d", "kind": "derived", "expr": "import os"}])
    # a valve tag is not a readable input
    with pytest.raises(ValidationError, match="not a physical input"):
        ChannelMap.model_validate(
            {
                "device": {"ip": "192.168.10.20", "watchdog_timeout_s": 5},
                "rate_groups": {"default": {"rate_hz": 50}},
                "channels": [
                    *_base_channels(),
                    {"tag": "valve.v", "hw": "FIO0", "kind": "valve", "fail_safe": "closed"},
                    {"tag": "calc.d", "kind": "derived", "expr": "valve.v + 1"},
                ],
            }
        )


# ------------------------------------------------- acquisition (cross-group)

MULTIRATE_YAML = """
device:
  ip: 192.168.10.20
  watchdog_timeout_s: 5
rate_groups:
  default: {rate_hz: 50}
  fast:    {rate_hz: 1000}
channels:
  - tag: pressure.fast
    hw: AIN0
    kind: analog_in
    unit: bar
    cal: {kind: linear, gain: 25.0, offset: 0.0}
    rate_group: fast
  - tag: pressure.slow
    hw: AIN1
    kind: analog_in
    unit: bar
    cal: {kind: linear, gain: 25.0, offset: 0.0}
    rate_group: default
  - tag: calc.sum
    kind: derived
    unit: bar
    expr: pressure.fast + pressure.slow
  - tag: calc.slow_double
    kind: derived
    unit: bar
    expr: pressure.slow * 2
"""


async def test_derived_acquisition_cross_group(tmp_path):
    p = tmp_path / "channels.yaml"
    p.write_text(MULTIRATE_YAML)
    chmap = load_channels(p).model
    # calc.sum runs on the fast group (fastest ref), calc.slow_double on default
    assert chmap.derived_group(chmap.by_tag("calc.sum")) == "fast"
    assert chmap.derived_group(chmap.by_tag("calc.slow_double")) == "default"

    mock = MockDevice(chmap)
    mock.open()
    mock.sim_set("pressure.fast", 10.0, noise_sigma_eu=0.0)
    mock.sim_set("pressure.slow", 3.0, noise_sigma_eu=0.0)
    acq = AcquisitionService(chmap, mock)
    assert acq.rings["fast"].tags == ["pressure.fast", "calc.sum"]
    assert acq.rings["default"].tags == ["pressure.slow", "calc.slow_double"]
    await acq.start()
    try:
        await asyncio.sleep(0.6)
    finally:
        await acq.stop()

    vals = acq.latest_values()
    # the mock adds a deterministic 1% sine wobble per channel: calc.sum can
    # legitimately swing 13.0 +/- 0.13 depending on wall-clock phase
    assert vals["calc.sum"] == pytest.approx(13.0, abs=0.2)
    assert vals["calc.slow_double"] == pytest.approx(6.0, abs=0.1)
    # live_window resolves derived tags too
    win = acq.live_window(["calc.sum"], 10.0)
    assert len(win["calc.sum"]["t"]) > 100  # ~1 kHz worth of samples


# ----------------------------------------------------------- end-to-end node


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


async def test_derived_end_to_end(client):
    chans = (await client.get("/api/channels")).json()["channels"]
    dp = next(c for c in chans if c["tag"] == "calc.dp_ox_inj")
    assert dp["kind"] == "derived" and dp["expr"] and dp["rate_hz"] == 50

    client.ctx.iface.sim_set("pressure.ox_manifold", 21.0, noise_sigma_eu=0.0)
    client.ctx.iface.sim_set("pressure.chamber", 15.0, noise_sigma_eu=0.0)
    await asyncio.sleep(0.5)
    live = (await client.get("/api/live?tags=calc.dp_ox_inj&window_s=5")).json()
    means = live["series"]["calc.dp_ox_inj"]["mean"]
    assert means and means[-1] == pytest.approx(6.0, abs=0.5)

    # recorded like any channel + exported to CSV
    await client.post("/api/runs/start", json={"name": "d", "operator": "op", "notes": ""})
    await asyncio.sleep(0.8)
    run_id = (await client.post("/api/runs/stop")).json()["run_id"]
    data = (await client.get(f"/api/runs/{run_id}/data?tags=calc.dp_ox_inj")).json()
    assert data["series"]["calc.dp_ox_inj"]["mean"]
    csv = (await client.get(f"/api/runs/{run_id}/export.csv?tags=calc.dp_ox_inj")).text
    assert csv.startswith("t_epoch,calc.dp_ox_inj")

    # redline on a derived channel validates and arms
    seq = (await client.get("/api/sequences/coldflow_v1/config")).json()["config"]
    seq["redlines"] = [{"tag": "calc.dp_ox_inj", "min": -50.0, "persist_ms": 50}]
    assert (await client.post("/api/sequences/validate", json=seq)).status_code == 200
