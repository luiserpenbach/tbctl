"""`tbctl-node`: asyncio composition root (spec §3).

Wires config -> device (real or simulator) -> acquisition -> API and runs
uvicorn. `--sim` swaps in the MockDevice with zero other changes (§9).
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from tbctl.config.loader import ConfigBundle, ConfigError, load_config_dir
from tbctl.services.acquisition import AcquisitionService

log = logging.getLogger(__name__)


@dataclass
class NodeContext:
    bundle: ConfigBundle
    iface: object  # DeviceInterface
    acq: AcquisitionService
    sim: bool
    control_enabled: bool = False
    data_dir: Path = field(default_factory=lambda: Path("data/runs"))


def build_context(config_dir: Path, sim: bool) -> NodeContext:
    bundle = load_config_dir(config_dir)
    chmap = bundle.channels.model
    if sim:
        from tbctl.hw.ljm_mock import MockDevice

        iface = MockDevice(chmap)
    else:
        from tbctl.hw.ljm_iface import LjmDevice

        iface = LjmDevice(chmap.device.ip)
    acq = AcquisitionService(chmap, iface)
    return NodeContext(bundle=bundle, iface=iface, acq=acq, sim=sim)


def build_app(ctx: NodeContext) -> FastAPI:
    from tbctl.api import rest, ws

    hub = ws.WsHub(ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        ctx.iface.open()
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
            await hub.stop()
            await ctx.acq.stop()
            ctx.iface.close()

    app = FastAPI(title="tbctl-node", lifespan=lifespan)
    app.state.ctx = ctx
    app.state.hub = hub
    app.include_router(rest.build_router(ctx))
    app.include_router(ws.build_router(ctx, hub))
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tbctl-node", description="testbench control node")
    parser.add_argument("--config-dir", default="configs")
    parser.add_argument("--sim", action="store_true", help="run against the built-in simulator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        ctx = build_context(Path(args.config_dir), args.sim)
    except ConfigError as e:
        print(f"config invalid:\n{e}", file=sys.stderr)
        return 1
    app = build_app(ctx)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
