// Audible abort alert (roadmap phase 15): operators look away from screens.
// WebAudio needs a user gesture before it can start, so the context is
// created lazily on the first pointer/key interaction — and resumed on later
// gestures if the browser suspended it. If audio still can't run when an
// abort fires (wall-mounted display nobody ever clicked), a full-screen
// visual flash makes sure the abort is never silent (M27).

let audioCtx: AudioContext | null = null;

function ensureAudio() {
  if (!audioCtx) {
    try {
      audioCtx = new AudioContext();
    } catch {
      audioCtx = null;
    }
  }
  // called from a user gesture — the one moment resume() is allowed to work
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
}

for (const ev of ["pointerdown", "keydown"]) {
  // not {once: true}: a later gesture must be able to resume a context the
  // browser suspended after creation
  window.addEventListener(ev, ensureAudio, { passive: true });
}

function beep(at: number, freq: number, dur = 0.14) {
  if (!audioCtx) return;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  osc.type = "square";
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.12, at);
  gain.gain.exponentialRampToValueAtTime(0.001, at + dur);
  osc.connect(gain).connect(audioCtx.destination);
  osc.start(at);
  osc.stop(at + dur);
}

function playAbortBeeps() {
  if (!audioCtx) return;
  const t = audioCtx.currentTime;
  for (let i = 0; i < 3; i++) beep(t + i * 0.22, 880);
}

/** Prominent visual fallback so an abort can never be silent AND invisible. */
function visualAbortFlash() {
  const id = "tbctl-abort-flash";
  if (document.getElementById(id)) return;
  const el = document.createElement("div");
  el.id = id;
  el.className = "abort-flash";
  el.textContent = "ABORT";
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

export function abortSound() {
  if (audioCtx && audioCtx.state === "running") {
    playAbortBeeps();
    return;
  }
  // audio unavailable or suspended: flash immediately, then still try to
  // resume — if the browser allows it, the chime plays too
  visualAbortFlash();
  if (audioCtx) {
    audioCtx
      .resume()
      .then(() => {
        if (audioCtx && audioCtx.state === "running") playAbortBeeps();
      })
      .catch(() => {});
  }
}

const ALARM_STATES = new Set(["ABORTING", "ABORTED", "FAULT"]);
let lastState = "";

/** Call on every sequencer state sample; sounds once per transition INTO an
 * alarm state. */
export function trackSeqState(state: string) {
  if (ALARM_STATES.has(state) && !ALARM_STATES.has(lastState) && lastState !== "") {
    abortSound();
  }
  lastState = state;
}
