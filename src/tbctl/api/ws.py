"""WebSocket publisher (spec §7): server->client msgpack messages.

Messages: `frame` (live data, ~10 Hz), `seq_status` (sequencer register
mirror, ~10 Hz), `event` (lifecycle events). Client->server (JSON text):
{"op": "subscribe"|"unsubscribe", "tags": [...]}. No control over WS —
all control is REST (D6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import TYPE_CHECKING

import msgpack
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from tbctl.hw.registers_gen import REG_NAME, SeqError, SeqState

if TYPE_CHECKING:
    from tbctl.node import NodeContext

log = logging.getLogger(__name__)

EMIT_INTERVAL_S = 0.1  # 10 Hz


class WsClient:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.tags: set[str] | None = None  # None = all
        self.t_last = time.time()

    async def send(self, msg: dict) -> None:
        await self.ws.send_bytes(msgpack.packb(msg, use_single_float=True))


class WsHub:
    def __init__(self, ctx: NodeContext):
        self._ctx = ctx
        self._clients: set[WsClient] = set()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._emit_loop(), name="ws-emit")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def broadcast_event(self, event: str, **payload) -> None:
        msg = {"type": "event", "event": event, "time": time.time(), **payload}
        for c in list(self._clients):
            with contextlib.suppress(Exception):
                await c.send(msg)

    def _seq_status(self) -> dict:
        iface = self._ctx.iface
        regs = iface.read_names(
            [
                REG_NAME["STATE"],
                REG_NAME["ERROR_CODE"],
                REG_NAME["CUR_STEP"],
                REG_NAME["ELAPSED_MS"],
                REG_NAME["STEP_COUNT"],
                REG_NAME["CRC16"],
            ]
        )
        return {
            "type": "seq_status",
            "time": time.time(),
            "state": SeqState(int(regs[0])).name,
            "error": SeqError(int(regs[1])).name,
            "cur_step": int(regs[2]),
            "elapsed_ms": int(regs[3]),
            "step_count": int(regs[4]),
            "crc16": int(regs[5]),
            "dio_state": int(iface.read_name("DIO_STATE")),
        }

    async def _emit_loop(self) -> None:
        while True:
            await asyncio.sleep(EMIT_INTERVAL_S)
            try:
                # stop the heartbeat when the sequencer reaches a terminal state
                await self._ctx.sequencer.watch_terminal()
            except Exception:
                log.exception("sequencer terminal watch failed")
            if not self._clients:
                continue
            try:
                seq_status = self._seq_status()
            except Exception:
                log.exception("seq status read failed")
                seq_status = None
            for c in list(self._clients):
                try:
                    now = time.time()
                    frames = self._ctx.acq.frames_since(
                        c.t_last, None if c.tags is None else sorted(c.tags)
                    )
                    c.t_last = now
                    for f in frames:
                        await c.send(f)
                    if seq_status:
                        await c.send(seq_status)
                except Exception:
                    self._clients.discard(c)

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        client = WsClient(ws)
        self._clients.add(client)
        try:
            while True:
                text = await ws.receive_text()
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                op, tags = msg.get("op"), set(msg.get("tags", []))
                if op == "subscribe":
                    client.tags = tags if tags else None
                elif op == "unsubscribe" and client.tags is not None:
                    client.tags -= tags
        except WebSocketDisconnect:
            pass
        finally:
            self._clients.discard(client)


def build_router(ctx: NodeContext, hub: WsHub) -> APIRouter:
    r = APIRouter()

    @r.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.handle(ws)

    return r
