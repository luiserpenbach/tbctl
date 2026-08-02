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
# Per-client outbound queue depth. A client that stops reading fills this and
# then we drop its oldest frames rather than letting one slow/wedged socket
# block the shared broadcast loop (and the sequencer terminal watch with it).
CLIENT_QUEUE_MAX = 256
SEND_TIMEOUT_S = 5.0


class WsClient:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.tags: set[str] | None = None  # None = all
        # per-rate-group send watermark; unseen groups start at connect time so
        # a new client is not dumped the entire ring history
        self.connect_t = time.time()
        self.t_last: dict[str, float] = {}
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=CLIENT_QUEUE_MAX)
        self._writer: asyncio.Task | None = None
        self.dropped = 0

    def enqueue(self, msg: dict) -> None:
        """Non-blocking hand-off to this client's writer task. Never awaits, so
        a slow consumer cannot stall the broadcast loop; on overflow the oldest
        queued frame is discarded."""
        # Keep IEEE-754 binary64 for epoch timestamps. float32 near 1e9 s only
        # resolves ~128 s, which collapses every sample in a frame onto one `t`
        # and the frontend drops non-increasing timestamps.
        data = msgpack.packb(msg, use_single_float=False)
        while True:
            try:
                self._queue.put_nowait(data)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                    self.dropped += 1

    def start_writer(self) -> None:
        self._writer = asyncio.create_task(self._write_loop(), name="ws-client-writer")

    async def _write_loop(self) -> None:
        try:
            while True:
                data = await self._queue.get()
                await asyncio.wait_for(self.ws.send_bytes(data), timeout=SEND_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            # send failed or timed out (dead/wedged socket): close it so the
            # receive loop in handle() unblocks and removes this client
            with contextlib.suppress(Exception):
                await self.ws.close()

    async def stop_writer(self) -> None:
        if self._writer:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
            self._writer = None


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
                c.enqueue(msg)

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
        loaded = self._ctx.sequencer.loaded
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
            # loaded sequence identity so the console can gate ARM/FIRE on the
            # sequence actually on the device, not the dropdown selection
            "loaded_sequence": loaded.name if loaded else None,
            "loaded_sha256": loaded.sha256 if loaded else None,
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
                # device register reads are a round-trip on real hardware; keep
                # them off the event loop so a wedged link can't stall it
                seq_status = await asyncio.to_thread(self._seq_status)
            except Exception:
                log.exception("seq status read failed")
                seq_status = None
            for c in list(self._clients):
                try:
                    frames = self._ctx.acq.frames_since(
                        c.t_last,
                        None if c.tags is None else sorted(c.tags),
                        default_t=c.connect_t,
                    )
                    for f in frames:
                        # Advance the per-group watermark from sample times, not
                        # wall clock — otherwise late-ingested points with
                        # t < now are skipped forever, and a shared cursor would
                        # let the fast group starve the slow one.
                        g = f.get("group", "")
                        if f.get("t"):
                            c.t_last[g] = max(c.t_last.get(g, c.connect_t), float(f["t"][-1]))
                        c.enqueue(f)
                    if seq_status:
                        c.enqueue(seq_status)
                except Exception:
                    log.exception("frame enqueue failed; dropping client")
                    self._clients.discard(c)

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        client = WsClient(ws)
        client.start_writer()
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
            await client.stop_writer()


def build_router(ctx: NodeContext, hub: WsHub) -> APIRouter:
    r = APIRouter()

    @r.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.handle(ws)

    return r
