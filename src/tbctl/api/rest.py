"""REST endpoints (spec §7): status, channels, live data, sequencer control,
manual valves, control mode, runs, dashboards. All control is REST — never
WebSocket (D6)."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Annotated, Literal

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


class ConnectionSwitch(BaseModel):
    mode: Literal["sim", "usb", "ethernet"]
    ip: str | None = None


class PidAssetUpload(BaseModel):
    name: str
    svg: str


class RunNote(BaseModel):
    text: str


class ChecklistActivate(BaseModel):
    name: str | None  # None = deactivate


class ChecklistCheck(BaseModel):
    index: int
    checked: bool


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
                "sim": ctx.iface.is_sim,
                "mode": ctx.device_mgr.mode,
            },
            "sequencer": {
                "state": SeqState(int(regs[0])).name,
                "error": SeqError(int(regs[1])).name,
                "cur_step": int(regs[2]),
                "elapsed_ms": int(regs[3]),
                "interp_version": int(regs[4]),
                "redlines_armed": ctx.sequencer.redlines.armed,
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
            "checklist": (
                {
                    "name": ctx.checklist["name"],
                    "total": len(ctx.checklist["items"]),
                    "checked": sum(ctx.checklist["checked"]),
                }
                if ctx.checklist
                else None
            ),
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

    @r.get("/events")
    def events(limit: Annotated[int, Query(gt=0, le=500)] = 100) -> dict:
        return {"events": ctx.events.tail(limit)}

    @r.get("/raw")
    def raw() -> dict:
        """Raw-vs-EU snapshot of every input channel (wiring checkout)."""
        chmap = ctx.bundle.channels.model
        raw_v = ctx.acq.latest_raw()
        eu_v = ctx.acq.latest_values()
        rows = []
        for c in chmap.inputs():
            rows.append(
                {
                    "tag": c.tag,
                    "hw": c.hw,
                    "kind": c.kind,
                    "unit": c.unit,
                    "rate_hz": chmap.rate_groups[c.rate_group].rate_hz,
                    "raw": raw_v.get(c.tag),
                    "value": eu_v.get(c.tag),
                }
            )
        return {"channels": rows}

    @r.get("/sim/setpoints")
    def sim_setpoints() -> dict:
        if not ctx.iface.is_sim:
            raise HTTPException(409, "not in simulator mode")
        return {"setpoints": ctx.iface.sim_setpoints()}

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
                    "redlines": [
                        {"tag": r.tag, "min": r.min, "max": r.max, "persist_ms": r.persist_ms}
                        for r in s.model.redlines
                    ],
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

    # ---------------------------------------------- sequence editor (ph. 12)

    _SEQ_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
    # POST /sequences/{name} would otherwise shadow these verbs for a
    # sequence that happened to share the name
    _SEQ_RESERVED = {"validate", "arm", "fire", "abort", "reset", "preconditions", "load"}

    def _seq_editable(name: str) -> bool:
        """A sequence is read-only while it is the one on the device and the
        sequencer has left IDLE (loaded/armed/running)."""
        loaded = ctx.sequencer.loaded
        if loaded is None or loaded.name != name:
            return True
        return ctx.sequencer.state() == SeqState.IDLE

    def _compile_or_422(payload: dict):
        from pydantic import ValidationError

        from tbctl.services.compiler import CompileError
        from tbctl.services.config_editor import compile_sequence_payload

        try:
            return compile_sequence_payload(ctx, payload)
        except ValidationError as e:
            raise HTTPException(
                422,
                [
                    {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
                    for err in e.errors()
                ],
            ) from e
        except CompileError as e:
            raise HTTPException(422, [{"loc": "compile", "msg": m} for m in e.errors]) from e

    @r.get("/sequences/{name}/config")
    def seq_config_get(name: str) -> dict:
        if name not in ctx.bundle.sequences:
            raise HTTPException(404, f"unknown sequence {name!r}")
        loaded = ctx.bundle.sequences[name]
        return {
            "config": loaded.model.model_dump(exclude_none=True),
            "sha256": loaded.sha256,
            "path": str(loaded.path),
            "editable": _seq_editable(name),
        }

    @r.post("/sequences/validate")
    def seq_validate(payload: dict) -> dict:
        """Dry-run compile: report + timeline preview, nothing written."""
        from tbctl.services.config_editor import sequence_timeline

        _, compiled = _compile_or_422(payload)
        return {
            "ok": True,
            "report": compiled.report(),
            "timeline": sequence_timeline(ctx, compiled),
        }

    @r.post("/sequences/{name}")
    def seq_save(name: str, payload: dict) -> dict:
        from tbctl.services.config_editor import apply_sequence

        if not _SEQ_NAME_RE.match(name) or name in _SEQ_RESERVED:
            raise HTTPException(400, f"bad sequence name {name!r}")
        if not _seq_editable(name):
            raise HTTPException(409, f"sequence {name!r} is loaded on the device; reset first")
        payload["name"] = name  # save-as: the URL wins
        _compile_or_422(payload)
        return apply_sequence(ctx, name, payload)

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

    @r.post("/runs/note")
    def runs_note(body: RunNote) -> dict:
        """Timestamped bookmark during a run -> JSONL + the run's HDF5."""
        if ctx.recorder.active is None:
            raise HTTPException(409, "no active run")
        text = body.text.strip()
        if not text:
            raise HTTPException(400, "empty note")
        ctx.events.log("note", text=text[:500], run_id=ctx.recorder.active.run_id)
        return {"ok": True}

    @r.get("/runs")
    def runs_list() -> dict:
        return {"runs": ctx.recorder.list_runs()}

    @r.get("/runs/{run_id}")
    def run_meta(run_id: str) -> dict:
        try:
            return ctx.recorder.run_meta(run_id)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e

    @r.get("/runs/{run_id}/events")
    def run_events(run_id: str) -> dict:
        from tbctl.services.recorder import RecorderError

        try:
            return {"events": ctx.recorder.run_events(run_id)}
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RecorderError as e:
            raise HTTPException(409, str(e)) from e

    @r.get("/runs/{run_id}/export.csv")
    def run_export_csv(
        run_id: str,
        tags: str,
        t0: float | None = None,
        t1: float | None = None,
    ):
        from fastapi.responses import StreamingResponse

        from tbctl.services.recorder import RecorderError

        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        if not tag_list:
            raise HTTPException(400, "no tags given")
        try:
            rows = ctx.recorder.export_csv_rows(run_id, tag_list, t0, t1)
            first = next(rows)  # touch the generator so 404s happen before streaming
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except RecorderError as e:
            raise HTTPException(409, str(e)) from e

        def stream():
            yield first
            yield from rows

        return StreamingResponse(
            stream(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{run_id}.csv"'},
        )

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

    @r.delete("/dashboards/{name}")
    def dashboard_delete(name: str) -> dict:
        if name not in ctx.bundle.dashboards:
            raise HTTPException(404, f"unknown dashboard {name!r}")
        if len(ctx.bundle.dashboards) <= 1:
            raise HTTPException(409, "cannot delete the last dashboard")
        path = ctx.bundle.dashboards[name].path
        path.unlink(missing_ok=True)
        del ctx.bundle.dashboards[name]
        ctx.events.log("dashboard_deleted", name=name)
        return {"ok": True}

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

    @r.post("/sim/set")
    def sim_set(body: SimSet) -> dict:
        if not ctx.iface.is_sim:
            raise HTTPException(409, "not in simulator mode")
        try:
            ctx.iface.sim_set(body.tag, body.value, body.noise_sigma)
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True, "setpoints": ctx.iface.sim_setpoints()}

    # ----------------------------------------------------- config editor

    @r.get("/config/channels")
    def config_channels_get() -> dict:
        loaded = ctx.bundle.channels
        return {
            "config": loaded.model.model_dump(exclude_none=True),
            "sha256": loaded.sha256,
            "path": str(loaded.path),
        }

    @r.post("/config/channels")
    async def config_channels_save(payload: dict) -> dict:
        from pydantic import ValidationError

        from tbctl.services.config_editor import apply_channels, validate_channels_payload

        _switch_guard()
        try:
            chmap, errors = validate_channels_payload(ctx, payload)
        except ValidationError as e:
            raise HTTPException(
                422,
                [
                    {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
                    for err in e.errors()
                ],
            ) from e
        if errors:
            raise HTTPException(422, [{"loc": "sequences", "msg": m} for m in errors])
        return await apply_channels(ctx, chmap, payload)

    # ------------------------------------------------ checklists (phase 15)

    def _read_checklists() -> dict[str, list[str]]:
        import yaml

        out: dict[str, list[str]] = {}
        d = ctx.config_dir / "checklists"
        if not d.is_dir():
            return out
        for path in sorted(d.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
                name = str(data["name"])
                items = [str(x) for x in data["items"]]
                if name and items:
                    out[name] = items
            except Exception:
                continue  # malformed checklist files are skipped, not fatal
        return out

    @r.get("/checklists")
    def checklists() -> dict:
        return {
            "checklists": [{"name": n, "items": i} for n, i in _read_checklists().items()],
            "active": ctx.checklist,
        }

    @r.post("/checklists/activate")
    def checklist_activate(body: ChecklistActivate) -> dict:
        if body.name is None:
            ctx.checklist = None
            ctx.events.log("checklist_deactivated")
            return {"active": None}
        all_lists = _read_checklists()
        if body.name not in all_lists:
            raise HTTPException(404, f"unknown checklist {body.name!r}")
        items = all_lists[body.name]
        ctx.checklist = {"name": body.name, "items": items, "checked": [False] * len(items)}
        ctx.events.log("checklist_activated", checklist=body.name, items=len(items))
        return {"active": ctx.checklist}

    @r.post("/checklists/check")
    def checklist_check(body: ChecklistCheck) -> dict:
        if ctx.checklist is None:
            raise HTTPException(409, "no active checklist")
        if not 0 <= body.index < len(ctx.checklist["items"]):
            raise HTTPException(400, "bad item index")
        ctx.checklist["checked"][body.index] = body.checked
        done = sum(ctx.checklist["checked"])
        ctx.events.log(
            "checklist_item",
            checklist=ctx.checklist["name"],
            item=ctx.checklist["items"][body.index],
            checked=body.checked,
            progress=f"{done}/{len(ctx.checklist['items'])}",
        )
        return {"active": ctx.checklist}

    # ---------------------------------------------------- P&ID (phase 13)

    _PID_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")
    _ASSET_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

    def _pid_assets_dir():
        return ctx.config_dir / "pid" / "assets"

    @r.get("/pid")
    def pid_list() -> dict:
        return {
            "pids": [
                {
                    "name": name,
                    "background": p.model.background,
                    "n_elements": len(p.model.elements),
                }
                for name, p in sorted(ctx.bundle.pids.items())
            ]
        }

    @r.get("/pid/assets")
    def pid_assets() -> dict:
        d = _pid_assets_dir()
        return {"assets": sorted(p.name for p in d.glob("*.svg")) if d.is_dir() else []}

    @r.post("/pid/assets")
    def pid_asset_upload(body: PidAssetUpload) -> dict:
        from tbctl.services.config_editor import sanitize_svg

        if not _ASSET_NAME_RE.match(body.name):
            raise HTTPException(400, "asset name must be [a-zA-Z0-9_-]+ (no extension)")
        if problems := sanitize_svg(body.svg):
            raise HTTPException(422, [{"loc": "svg", "msg": m} for m in problems])
        d = _pid_assets_dir()
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{body.name}.svg"
        (d / fname).write_text(body.svg)
        ctx.events.log("pid_asset_uploaded", asset=fname, bytes=len(body.svg))
        return {"ok": True, "asset": fname}

    @r.get("/pid/assets/{fname}")
    def pid_asset_get(fname: str):
        from fastapi.responses import Response

        if not re.match(r"^[a-zA-Z0-9_\-]+\.svg$", fname):
            raise HTTPException(400, "bad asset name")
        path = _pid_assets_dir() / fname
        if not path.is_file():
            raise HTTPException(404, f"no asset {fname!r}")
        return Response(path.read_text(), media_type="image/svg+xml")

    @r.get("/pid/{name}")
    def pid_get(name: str) -> dict:
        if name not in ctx.bundle.pids:
            raise HTTPException(404, f"unknown pid {name!r}")
        loaded = ctx.bundle.pids[name]
        return {
            "config": loaded.model.model_dump(exclude_none=True),
            "sha256": loaded.sha256,
            "path": str(loaded.path),
        }

    @r.post("/pid/{name}")
    def pid_save(name: str, payload: dict) -> dict:
        from pydantic import ValidationError

        from tbctl.services.config_editor import apply_pid, validate_pid_payload

        if not _PID_NAME_RE.match(name) or name == "assets":
            raise HTTPException(400, f"bad pid name {name!r}")
        payload["name"] = name  # save-as: the URL wins
        try:
            _, errors = validate_pid_payload(ctx, payload)
        except ValidationError as e:
            raise HTTPException(
                422,
                [
                    {"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]}
                    for err in e.errors()
                ],
            ) from e
        if errors:
            raise HTTPException(422, [{"loc": "channels", "msg": m} for m in errors])
        return apply_pid(ctx, name, payload)

    @r.delete("/pid/{name}")
    def pid_delete(name: str) -> dict:
        if name not in ctx.bundle.pids:
            raise HTTPException(404, f"unknown pid {name!r}")
        from tbctl.services.config_editor import backup_file

        path = ctx.bundle.pids[name].path
        backup_file(path)
        path.unlink(missing_ok=True)
        del ctx.bundle.pids[name]
        ctx.events.log("pid_deleted", pid=name)
        return {"ok": True}

    # -------------------------------------------------------- connection

    def _switch_guard() -> None:
        st = ctx.sequencer.state()
        if st != SeqState.IDLE:
            raise HTTPException(409, f"sequencer is {st.name}; must be IDLE")
        if ctx.recorder.active:
            raise HTTPException(409, "recording in progress")
        if ctx.control_enabled:
            raise HTTPException(409, "disable control mode first")

    @r.get("/connection")
    def connection() -> dict:
        return ctx.device_mgr.info()

    @r.post("/connection")
    async def connection_switch(body: ConnectionSwitch) -> dict:
        _switch_guard()
        try:
            await ctx.device_mgr.switch(body.mode, body.ip)
        except Exception as e:
            raise HTTPException(502, f"switch failed (previous connection restored): {e}") from e
        ctx.sim = ctx.iface.is_sim
        return ctx.device_mgr.info()

    @r.post("/connection/deploy-lua")
    async def connection_deploy() -> dict:
        import asyncio

        from tbctl.hw.script_loader import ScriptLoadError, deploy_script, read_debug
        from tbctl.hw.watchdog import configure_watchdog

        if ctx.iface.is_sim:
            raise HTTPException(409, "deploy not applicable in simulator mode")
        _switch_guard()
        try:
            version = await asyncio.to_thread(deploy_script, ctx.iface)
            await asyncio.to_thread(configure_watchdog, ctx.iface, ctx.bundle.channels.model)
        except ScriptLoadError as e:
            import contextlib

            debug = ""
            with contextlib.suppress(Exception):
                debug = await asyncio.to_thread(read_debug, ctx.iface)
            raise HTTPException(502, f"deploy failed: {e}\nLUA_DEBUG: {debug}") from e
        ctx.events.log("lua_deployed", interp_version=version)
        return {"ok": True, "interp_version": version}

    @r.get("/connection/read")
    def connection_read(name: str) -> dict:
        """One-shot register peek by LJM name (wiring checkout)."""
        if not re.match(r"^[A-Za-z0-9_#]+$", name):
            raise HTTPException(400, "bad register name")
        try:
            value = ctx.iface.read_name(name)
        except Exception as e:
            raise HTTPException(400, f"read failed: {e}") from e
        return {"name": name, "value": value}

    return r
