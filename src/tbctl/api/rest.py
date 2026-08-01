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


class RunStart(BaseModel):
    name: str
    operator: str
    notes: str = ""


class Confirm(BaseModel):
    confirm: str


class ManualValve(BaseModel):
    tag: str
    state: str


class ControlMode(BaseModel):
    enabled: bool


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
            "recording": {
                "active": ctx.recorder.active is not None,
                "run_id": ctx.recorder.active.run_id if ctx.recorder.active else None,
                "t_start": ctx.recorder.active.t_start if ctx.recorder.active else None,
            },
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
                entry["active_level"] = chmap.active_level(c)
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

    # -------------------------------------------------------- sequencer

    @r.get("/sequences")
    def sequences() -> dict:
        out = []
        for name, s in sorted(ctx.bundle.sequences.items()):
            out.append(
                {
                    "name": name,
                    "description": s.model.description,
                    "n_steps": len(s.model.steps),
                    "n_abort_steps": len(s.model.abort),
                    "max_duration_s": s.model.max_duration_s,
                    "allow_unsafe_end": s.model.allow_unsafe_end,
                    "sha256": s.sha256,
                    "loaded": ctx.sequencer.loaded is not None
                    and ctx.sequencer.loaded.name == name,
                }
            )
        return {"sequences": out}

    def _seq_err(e: Exception) -> HTTPException:
        return HTTPException(409, str(e))

    @r.post("/sequences/{name}/load")
    async def seq_load(name: str) -> dict:
        from tbctl.services.compiler import CompileError
        from tbctl.services.sequencer import SequencerError

        try:
            report = await ctx.sequencer.load(name)
        except CompileError as e:
            raise HTTPException(422, str(e)) from e
        except SequencerError as e:
            raise _seq_err(e) from e
        ctx.recorder.sequence_sha256 = ctx.sequencer.loaded.sha256
        return report

    @r.post("/sequences/arm")
    async def seq_arm(body: Confirm) -> dict:
        from tbctl.services.sequencer import SequencerError

        try:
            await ctx.sequencer.arm(body.confirm, ctx.control_enabled)
        except SequencerError as e:
            raise _seq_err(e) from e
        return {"ok": True, "state": ctx.sequencer.state().name}

    @r.post("/sequences/fire")
    async def seq_fire(body: Confirm) -> dict:
        from tbctl.services.sequencer import SequencerError

        try:
            await ctx.sequencer.fire(body.confirm)
        except SequencerError as e:
            raise _seq_err(e) from e
        return {"ok": True, "state": ctx.sequencer.state().name}

    @r.post("/sequences/abort")
    async def seq_abort() -> dict:
        # ALWAYS accepted, no body, fastest path in the API (§7)
        await ctx.sequencer.abort()
        return {"ok": True}

    @r.post("/sequences/reset")
    async def seq_reset() -> dict:
        from tbctl.services.sequencer import SequencerError

        try:
            await ctx.sequencer.reset()
        except SequencerError as e:
            raise _seq_err(e) from e
        return {"ok": True, "state": ctx.sequencer.state().name}

    @r.get("/sequences/preconditions")
    def seq_preconditions() -> dict:
        return {"failures": ctx.sequencer.check_preconditions()}

    # ---------------------------------------------------- manual control

    @r.post("/manual/valve")
    def manual_valve(body: ManualValve) -> dict:
        from tbctl.services.manual_control import ManualControlError

        try:
            return ctx.manual.set_output(body.tag, body.state, ctx.control_enabled)
        except ManualControlError as e:
            raise HTTPException(409, str(e)) from e

    @r.post("/control-mode")
    def control_mode(body: ControlMode) -> dict:
        ctx.control_enabled = body.enabled
        ctx.events.log("control_mode", enabled=body.enabled)
        return {"enabled": ctx.control_enabled}

    # ------------------------------------------------------------- runs

    @r.post("/runs/start")
    async def runs_start(body: RunStart) -> dict:
        from tbctl.services.recorder import RecorderError

        try:
            run = await ctx.recorder.start_run(body.name, body.operator, body.notes)
        except RecorderError as e:
            raise HTTPException(409, str(e)) from e
        ctx.events.log("run_started", run_id=run.run_id, name=run.name, operator=run.operator)
        return run.to_dict()

    @r.post("/runs/stop")
    async def runs_stop() -> dict:
        from tbctl.services.recorder import RecorderError

        try:
            run = await ctx.recorder.stop_run()
        except RecorderError as e:
            raise HTTPException(409, str(e)) from e
        ctx.events.log("run_stopped", run_id=run.run_id, sample_counts=run.sample_counts)
        return run.to_dict()

    @r.get("/runs")
    def runs_list() -> dict:
        return {"runs": ctx.recorder.list_runs()}

    @r.get("/runs/{run_id}")
    def run_meta(run_id: str) -> dict:
        try:
            return ctx.recorder.run_meta(run_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @r.get("/runs/{run_id}/data")
    def run_data(
        run_id: str,
        tags: str,
        t0: float | None = None,
        t1: float | None = None,
        max_points: Annotated[int, Query(gt=1, le=20000)] = 2000,
    ) -> dict:
        from tbctl.services.recorder import RecorderError

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not tag_list:
            raise HTTPException(400, "no tags given")
        try:
            series = ctx.recorder.run_data(run_id, tag_list, t0, t1, max_points)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RecorderError as e:
            raise HTTPException(409, str(e)) from e
        return {"run_id": run_id, "series": series}

    # -------------------------------------------------------- dashboards

    @r.get("/dashboards")
    def dashboards() -> dict:
        return {"dashboards": sorted(ctx.bundle.dashboards.keys())}

    @r.get("/dashboards/{name}")
    def dashboard(name: str) -> dict:
        if name not in ctx.bundle.dashboards:
            raise HTTPException(404, f"unknown dashboard {name!r}")
        return ctx.bundle.dashboards[name].model.model_dump()

    @r.post("/dashboards/{name}")
    def dashboard_save(name: str, body: dict) -> dict:
        """Edit-mode save: validate and write back as YAML (§4.3)."""
        import re as _re

        import yaml

        from tbctl.config.loader import load_dashboard
        from tbctl.config.models import Dashboard

        if not _re.match(r"^[a-zA-Z0-9_\-]+$", name):
            raise HTTPException(400, "bad dashboard name")
        body["name"] = name
        try:
            model = Dashboard.model_validate(body)
        except Exception as e:
            raise HTTPException(422, str(e)) from e
        path = ctx.config_dir / "dashboards" / f"{name}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(model.model_dump(), sort_keys=False))
        ctx.bundle.dashboards[name] = load_dashboard(path)
        ctx.events.log("dashboard_saved", name=name)
        return {"ok": True, "path": str(path)}

    if ctx.sim:

        @r.post("/sim/set")
        def sim_set(body: SimSet) -> dict:
            try:
                ctx.iface.sim_set(body.tag, body.value, body.noise_sigma)
            except KeyError as e:
                raise HTTPException(404, str(e)) from e
            return {"ok": True, "setpoints": ctx.iface.sim_setpoints()}

    return r
