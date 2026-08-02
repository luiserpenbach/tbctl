"""Channel config editor: validated writes, backups, hot-apply, guards,
transactional failure-restore, and the P&ID SVG allowlist sanitizer."""

import asyncio
import shutil

import httpx
import pytest
import yaml

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
        {"tag": "pressure.new_pt", "hw": "AIN7", "kind": "analog_in", "unit": "bar"}
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


# ------------------------------------------------- C5: transactional apply


async def test_indivisible_rate_group_rejected_before_write(client):
    """C5 (validation half): the report's reproducer — a 7 Hz group that
    cannot integer-decimate from the stream rate must 422, not brick the node."""
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["rate_groups"]["odd"] = {"rate_hz": 7}
    for c in cfg["channels"]:
        if c["tag"] == "pressure.chamber":
            c["rate_group"] = "odd"
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    assert "divide" in str(r.json())
    # nothing persisted, acquisition untouched
    assert "odd" not in (client.cfg / "channels.yaml").read_text()
    assert client.ctx.acq.running


async def test_input_less_map_rejected(client):
    cfg = (await client.get("/api/config/channels")).json()["config"]
    cfg["channels"] = [c for c in cfg["channels"] if c["kind"] in ("valve", "spark")]
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    assert "at least one input channel" in str(r.json())


async def test_fail_safe_flip_caught_at_channel_edit_time(client):
    """M15(b): a fail_safe flip changes safe_word; every existing sequence
    must still COMPILE against the new map or the edit is rejected."""
    cfg = (await client.get("/api/config/channels")).json()["config"]
    for c in cfg["channels"]:
        if c["tag"] == "valve.purge":
            c["fail_safe"] = "open"
    r = await client.post("/api/config/channels", json=cfg)
    assert r.status_code == 422
    body = str(r.json())
    assert "sequence" in body and "safe_word" in body
    # nothing persisted
    assert (
        "fail_safe: open"
        not in ((client.cfg / "channels.yaml").read_text().split("valve.purge")[1][:80])
    )


async def test_failed_hot_apply_restores_previous_state(client, monkeypatch):
    """C5 (editor half): if the hot-apply blows up after the write, the old
    file must come back and the old config must be re-applied — never a dead
    node with the new file persisted."""
    before = (client.cfg / "channels.yaml").read_bytes()
    cfg = (await client.get("/api/config/channels")).json()["config"]
    for c in cfg["channels"]:
        if c["tag"] == "pressure.chamber":
            c["cal"] = {"kind": "linear", "gain": 123.0, "offset": 0.0}

    calls = {"n": 0}
    orig_start = client.ctx.acq.start

    async def boom():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated stream start failure")
        await orig_start()

    monkeypatch.setattr(client.ctx.acq, "start", boom)
    with pytest.raises(RuntimeError, match="simulated stream start failure"):
        await client.post("/api/config/channels", json=cfg)

    # file restored byte-for-byte, old model still live, acquisition restarted
    assert (client.cfg / "channels.yaml").read_bytes() == before
    assert client.ctx.bundle.channels.model.by_tag("pressure.chamber").cal.kind == "polynomial"
    assert calls["n"] == 2  # restore path re-applied the old config
    assert client.ctx.acq.running
    # node is still fully operational: the same (valid) edit now succeeds
    monkeypatch.setattr(client.ctx.acq, "start", orig_start)
    assert (await client.post("/api/config/channels", json=cfg)).status_code == 200
    assert client.ctx.bundle.channels.model.by_tag("pressure.chamber").cal.kind == "linear"


async def test_written_file_is_validated_model_dump(client):
    """C5/M17: what lands on disk is the validated model's canonical dump,
    safe for vanilla YAML consumers (bool-like strings quoted)."""
    cfg = (await client.get("/api/config/channels")).json()["config"]
    assert (await client.post("/api/config/channels", json=cfg)).status_code == 200
    text = (client.cfg / "channels.yaml").read_text()
    # a STANDARD yaml parser must read fail_safe: off as the string "off"
    parsed = yaml.safe_load(text)
    spark = next(c for c in parsed["channels"] if c["kind"] == "spark")
    assert spark["fail_safe"] == "off"
    vents = [c for c in parsed["channels"] if c["tag"].startswith("valve.")]
    assert all(isinstance(c["fail_safe"], str) for c in vents)
    # and our own loader round-trips to the identical model
    from tbctl.config.loader import load_channels

    assert load_channels(client.cfg / "channels.yaml").model == client.ctx.bundle.channels.model
    # no stray temp files from the atomic write
    assert not list(client.cfg.glob(".channels.yaml.*"))


# ------------------------------------------------- M14: SVG allowlist


async def test_svg_sanitizer_rejects_bypasses(client):
    """The report's bypass catalogue: <style> CSS, entity tricks, external
    use/image hrefs — all must be rejected by the allowlist sanitizer."""
    evil = [
        # style element smuggles CSS (external fetches, UI redress)
        '<svg xmlns="http://www.w3.org/2000/svg"><style>text{fill:red}</style><rect/></svg>',
        # entity-encoded handler name is not well-formed XML -> reject
        '<svg><rect width="5" on&#99;lick="alert(1)"/></svg>',
        # case-shifted handler
        '<svg><rect width="5" ONCLICK="alert(1)"/></svg>',
        # external <use> href (both spellings)
        '<svg><use href="http://evil.example/sprite.svg#part"/></svg>',
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink"><use xlink:href="//evil/x.svg#p"/></svg>',
        # href on a non-use element, even local
        '<svg><a href="#x"><rect/></a></svg>',
        # image / foreignObject / script elements
        '<svg><image href="http://evil/x.png"/></svg>',
        "<svg><foreignObject><body/></foreignObject></svg>",
        "<svg><script>alert(1)</script></svg>",
        # animation elements
        '<svg><rect><animate attributeName="x" to="0"/></rect></svg>',
        # DTD/entity games
        '<!DOCTYPE svg [<!ENTITY x "<script>alert(1)</script>">]><svg>&x;</svg>',
        # external url() in a presentation attribute
        '<svg><rect fill="url(http://evil/#f)"/></svg>',
        # style attribute (inline CSS) is not in the attribute allowlist
        '<svg><rect style="fill:url(http://evil)"/></svg>',
        # root is not svg
        "<html>not svg</html>",
    ]
    for svg in evil:
        r = await client.post("/api/pid/assets", json={"name": "evil", "svg": svg})
        assert r.status_code == 422, f"accepted: {svg}"
    assert (await client.get("/api/pid/assets/evil.svg")).status_code == 404

    good = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<defs><marker id="m" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3z"/></marker></defs>'
        '<g stroke="#123" marker-end="url(#m)"><rect x="1" y="1" width="5" height="5"/>'
        '<use href="#m"/><text x="2" y="2" text-anchor="middle">OK</text></g></svg>'
    )
    r = await client.post("/api/pid/assets", json={"name": "okay", "svg": good})
    assert r.status_code == 200, r.text
    # the shipped background still uploads cleanly
    shipped = (client.cfg / "pid" / "assets" / "feed_system.svg").read_text()
    r = await client.post("/api/pid/assets", json={"name": "shipped", "svg": shipped})
    assert r.status_code == 200, r.text


def test_clean_svg_strips_and_serializes():
    from tbctl.config.loader import ConfigError
    from tbctl.services.config_editor import clean_svg, sanitize_svg

    out = clean_svg(
        '<svg xmlns="http://www.w3.org/2000/svg" onload="x">'
        '<script>bad()</script><rect width="5" fill="red" data-x="1"/></svg>'
    )
    assert "script" not in out and "onload" not in out and "data-x" not in out
    assert 'width="5"' in out and 'fill="red"' in out
    assert sanitize_svg(out) == []  # cleaned output is itself clean
    with pytest.raises(ConfigError):
        clean_svg("<html>nope</html>")
    with pytest.raises(ConfigError):
        clean_svg("<svg><unclosed</svg>")
