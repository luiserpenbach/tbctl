import { decode } from "@msgpack/msgpack";
import { buffers } from "./databuf";
import { useStore } from "./store";
import type { WsMsg } from "./types";

let retryMs = 500;
let started = false;

export function connectWs() {
  if (started) return; // React StrictMode double-mounts; connect once
  started = true;
  openSocket();
}

function openSocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws`);
  sock.binaryType = "arraybuffer";

  sock.onopen = () => {
    retryMs = 500;
    useStore.getState().setWsConnected(true);
    sock.send(JSON.stringify({ op: "subscribe", tags: [] })); // all tags
  };

  sock.onmessage = (ev) => {
    const msg = decode(new Uint8Array(ev.data as ArrayBuffer)) as WsMsg;
    if (msg.type === "frame") {
      buffers.appendFrame(msg);
    } else if (msg.type === "seq_status") {
      useStore.getState().setSeqStatus(msg);
    } else if (msg.type === "event") {
      useStore.getState().pushEvent(msg);
    }
  };

  sock.onclose = () => {
    useStore.getState().setWsConnected(false);
    setTimeout(openSocket, retryMs);
    retryMs = Math.min(retryMs * 2, 5000);
  };
  sock.onerror = () => sock.close();
}
