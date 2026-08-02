import { decode } from "@msgpack/msgpack";
import { buffers } from "./databuf";
import { useStore } from "./store";
import type { WsMsg } from "./types";

const BASE_RETRY_MS = 500;
const MAX_RETRY_MS = 5000;
// L1: a connection only proves the backend healthy after staying open a few
// seconds — resetting backoff in onopen lets a crash-looping backend get
// hammered at 500 ms by every client forever.
const STABLE_MS = 3000;

let retryMs = BASE_RETRY_MS;
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
  let stableTimer: ReturnType<typeof setTimeout> | null = null;

  sock.onopen = () => {
    stableTimer = setTimeout(() => {
      retryMs = BASE_RETRY_MS;
    }, STABLE_MS);
    const st = useStore.getState();
    st.setWsConnected(true);
    st.bumpWsGeneration(); // H9: consumers re-run their live backfill
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
    if (stableTimer !== null) clearTimeout(stableTimer);
    // H9: break the line at the outage so uPlot draws a gap, not a bridge
    buffers.markGap();
    useStore.getState().setWsConnected(false);
    // L1: small jitter so a room full of consoles doesn't reconnect in lockstep
    const delay = retryMs * (1 + Math.random() * 0.25);
    retryMs = Math.min(retryMs * 2, MAX_RETRY_MS);
    setTimeout(openSocket, delay);
  };
  sock.onerror = () => sock.close();
}
