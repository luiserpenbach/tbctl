"""REST endpoints (spec §7). M2 scope: status, channels, live. Control,
runs and dashboards land with their services (M4/M5)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from tbctl.hw.registers_gen import REG_NAME, SeqError, SeqState

if TYPE_CHECKING:
    from tbctl.node import NodeContext


class SimSet(BaseModel):
    tag: str
    value: float
    noise_sigma: float | None = None


def build_router(ctx: NodeContext) -> APIRouter:
    r = APIRouter(prefix="/api")

    @r.get("/status")
    def status() -> dict:
        regs = ctx.iface.read_names(
            [
                REG_NAME["STATE"],
                REG_NAME["ERROR_CODE"],
                REG_NAME["CUR_STEP"],
                REG_NAME["ELAPSED_MS"],
                REG_NAME["INTERP_VERSION"],
            ]
        )
        dio_state = int(ctx.iface.read_name("DIO_STATE"))
        chmap = ctx.bundle.channels.model
        valve_states = {}
        for c in chmap.outputs():
            from tbctl.config.models import dio_number

            level = (dio_state >> dio_number(c.hw)) & 1
            active = level == chmap.active_level(c)
            valve_states[c.tag] = (
                ("open" if active else "closed")
                if c.kind == "valve"
                else ("on" if active else "off")
            )
        return {
            "time": time.time(),
            "device": {
                "connected": ctx.iface.connected,
                "ip": chmap.device.ip,
                "sim": ctx.sim,
            },
            "sequencer": {
                "state": SeqState(int(regs[0])).name,
                "error": SeqError(int(regs[1])).name,
                "cur_step": int(regs[2]),
                "elapsed_ms": int(regs[3]),
                "interp_version": int(regs[4]),
            },
            "acquisition": {
                "running": ctx.acq.running,
                "scan_rate_hz": ctx.acq.scan_rate_hz,
                "sample_count": ctx.acq.sample_count,
            },
            "recording": {"active": False, "run_id": None},
            "control_enabled": ctx.control_enabled,
            "outputs": valve_states,
            "config": {
                "channels_sha256": ctx.bundle.channels.sha256,
                "sequences": {n: s.sha256 for n, s in ctx.bundle.sequences.items()},
            },
        }

    @r.get("/channels")
    def channels() -> dict:
        chmap = ctx.bundle.channels.model
        out = []
        for c in chmap.channels:
            entry: dict = {"tag": c.tag, "hw": c.hw, "kind": c.kind}
            if c.kind in ("analog_in", "thermocouple_k"):
                entry["unit"] = c.unit
                entry["rate_group"] = c.rate_group
                entry["rate_hz"] = chmap.rate_groups[c.rate_group].rate_hz
                if c.kind == "analog_in":
                    entry["cal"] = c.cal.model_dump()
            else:
                entry["fail_safe"] = c.fail_safe
                entry["bit"] = chmap.bit_index(c.tag)
            out.append(entry)
        return {
            "channels": out,
            "rate_groups": {k: v.rate_hz for k, v in chmap.rate_groups.items()},
        }

    @r.get("/live")
    def live(
        tags: str,
        window_s: Annotated[float, Query(gt=0, le=600)] = 60.0,
        max_points: Annotated[int, Query(gt=1, le=20000)] = 2000,
    ) -> dict:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not tag_list:
            raise HTTPException(400, "no tags given")
        try:
            data = ctx.acq.live_window(tag_list, window_s, max_points)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"window_s": window_s, "series": data}

    @r.get("/dashboards")
    def dashboards() -> dict:
        return {"dashboards": sorted(ctx.bundle.dashboards.keys())}

    @r.get("/dashboards/{name}")
    def dashboard(name: str) -> dict:
        if name not in ctx.bundle.dashboards:
            raise HTTPException(404, f"unknown dashboard {name!r}")
        return ctx.bundle.dashboards[name].model.model_dump()

    if ctx.sim:

        @r.post("/sim/set")
        def sim_set(body: SimSet) -> dict:
            try:
                ctx.iface.sim_set(body.tag, body.value, body.noise_sigma)
            except KeyError as e:
                raise HTTPException(404, str(e)) from e
            return {"ok": True, "setpoints": ctx.iface.sim_setpoints()}

    return r
