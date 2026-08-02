"""`tbctl-node`: asyncio composition root (spec §3).

Wires config -> device (real or simulator) -> acquisition -> API and runs
uvicorn. `--sim` swaps in the MockDevice with zero other changes (§9).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from tbctl.config.loader import ConfigBundle, ConfigError, load_config_dir
from tbctl.services.acquisition import AcquisitionService
from tbctl.services.events import EventLog
from tbctl.services.manual_control import ManualControl
from tbctl.services.recorder import Recorder
from tbctl.services.sequencer import SequencerService

log = logging.getLogger(__name__)


def _git_hash() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    except Exception:
        return "unknown"


@dataclass
class NodeContext:
    bundle: ConfigBundle
    iface: object  # DeviceInterface
    acq: AcquisitionService
    events: EventLog
    recorder: Recorder
    sequencer: SequencerService
    manual: ManualControl
    device_mgr: object  # DeviceManager
    sim: bool
    config_dir: Path
    control_enabled: bool = False
    data_dir: Path = field(default_factory=lambda: Path("data/runs"))
    # active pre-test checklist: {"name": str, "items": [str], "checked": [bool]}
    checklist: dict | None = None
    # serializes device-lifecycle operations (connection switch, lua deploy,
    # channel hot-apply, run start/stop) so their check-then-act guards can't
    # interleave across awaits and corrupt the stream/recorder state.
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_context(
    config_dir: Path,
    sim: bool,
    data_dir: Path | None = None,
    log_dir: Path | None = None,
) -> NodeContext:
    from tbctl.services.device_manager import DeviceManager, SwitchableDevice

    bundle = load_config_dir(config_dir)
    chmap = bundle.channels.model
    if sim:
        from tbctl.hw.ljm_mock import MockDevice

        backend = MockDevice(chmap)
        mode = "sim"
    else:
        from tbctl.hw.ljm_iface import LjmDevice

        conn, ident = chmap.device.ljm_open_args()
        backend = LjmDevice(ident, conn)
        mode = "usb" if chmap.device.connection == "usb" else "ethernet"
    iface = SwitchableDevice(backend)
    acq = AcquisitionService(chmap, iface)
    data_dir = data_dir or Path("data/runs")
    events = EventLog(log_dir or Path("logs"))
    recorder = Recorder(chmap, bundle.channels.sha256, acq, data_dir, _git_hash())
    events.add_sink(recorder.on_event)
    sequencer = SequencerService(bundle, iface, acq, events)
    manual = ManualControl(chmap, iface, events)
    device_mgr = DeviceManager(chmap, iface, acq, events, mode)
    ctx = NodeContext(
        device_mgr=device_mgr,
        bundle=bundle,
        iface=iface,
        acq=acq,
        events=events,
        recorder=recorder,
        sequencer=sequencer,
        manual=manual,
        sim=sim,
        config_dir=config_dir,
        data_dir=data_dir,
    )

    def _post_stop_review(run) -> None:
        """Evaluate review rules against the just-closed run (Sift-style
        automated data review); reads ctx.bundle so hot-applied config edits
        are picked up."""
        from tbctl.services.annotations import replace_rule_annotations
        from tbctl.services.review import evaluate_run, store_report

        review = ctx.bundle.review
        if review is None or not review.model.rules:
            return
        report = evaluate_run(review.model, run.path)
        store_report(run.path, report)
        replace_rule_annotations(run.path, report)
        events.log("run_report", run_id=run.run_id, **report["summary"])

    recorder.on_stop = _post_stop_review
    return ctx


def _install_auth(app: FastAPI) -> None:
    """Optional shared-token gate (spec: bench LAN is otherwise trusted).

    Off by default so the simulator, tests and an air-gapped stand are
    unaffected. When TBCTL_API_TOKEN is set in the environment, every request
    must present it as `Authorization: Bearer <token>`, an `X-API-Token`
    header, or a `?token=` query param (the last is for the WebSocket, which
    can't set headers from the browser). This is a coarse control-plane lock,
    not per-user auth — pair it with binding to a trusted interface.
    """
    import hmac
    import os

    token = os.environ.get("TBCTL_API_TOKEN")
    if not token:
        return

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    def _presented(request: Request) -> str | None:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return request.headers.get("x-api-token") or request.query_params.get("token")

    @app.middleware("http")
    async def _auth_mw(request: Request, call_next):
        supplied = _presented(request)
        if supplied is None or not hmac.compare_digest(supplied, token):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    log.info("API token auth enabled")


def build_app(ctx: NodeContext) -> FastAPI:
    from tbctl.api import rest, ws

    hub = ws.WsHub(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop = asyncio.get_running_loop()
        bg_tasks: set[asyncio.Task] = set()

        def _spawn_broadcast(record: dict) -> None:
            # keep a strong reference: the event loop only weakly references
            # tasks, so a bare create_task can be GC'd mid-flight
            task = loop.create_task(hub.broadcast_event(**record))
            bg_tasks.add(task)
            task.add_done_callback(bg_tasks.discard)

        def ws_sink(record: dict) -> None:
            loop.call_soon_threadsafe(_spawn_broadcast, record)

        remove_sink = ctx.events.add_sink(ws_sink)
        ctx.iface.open()
        if not ctx.sim:
            # real T7: verify the deployed interpreter and arm the hardware
            # watchdog (safety layer 2) before anything else touches DIO
            from tbctl.hw.watchdog import configure_watchdog

            await asyncio.to_thread(ctx.sequencer.check_interp_version)
            await asyncio.to_thread(configure_watchdog, ctx.iface, ctx.bundle.channels.model)
        await ctx.acq.start()
        hub.start()
        log.info(
            "node up: %s device, %d channels, stream %.0f Hz",
            "sim" if ctx.sim else "T7",
            len(ctx.bundle.channels.model.channels),
            ctx.acq.scan_rate_hz,
        )
        try:
            yield
        finally:
            await ctx.sequencer.shutdown()
            ctx.manual.shutdown()
            if ctx.recorder.active:
                await ctx.recorder.stop_run()
            remove_sink()
            await hub.stop()
            await ctx.acq.stop()
            ctx.iface.close()

    app = FastAPI(
        title="tbctl-node", lifespan=lifespan, default_response_class=rest.SafeJSONResponse
    )
    app.state.ctx = ctx
    app.state.hub = hub
    _install_auth(app)
    app.include_router(rest.build_router(ctx))
    app.include_router(ws.build_router(ctx, hub))

    # serve the built frontend (frontend/dist) when present, so the Pi needs
    # no separate web server — the browser just hits http://<pi>:8000/
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbctl-node", description="testbench control node")
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument("--sim", action="store_true", help="run against the built-in simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--data-dir", default="data/runs", help="run HDF5 directory (USB SSD)")
    parser.add_argument("--log-dir", default="logs", help="event JSONL directory")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        ctx = build_context(
            Path(args.config_dir),
            args.sim,
            data_dir=Path(args.data_dir),
            log_dir=Path(args.log_dir),
        )
    except ConfigError as e:
        print(f"config invalid:\n{e}", file=sys.stderr)
        return 1
    app = build_app(ctx)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
