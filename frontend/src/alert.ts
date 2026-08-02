// Audible abort alert (roadmap phase 15): operators look away from screens.
// WebAudio needs a user gesture before it can start, so the context is
// created lazily on the first pointer/key interaction.

let audioCtx: AudioContext | null = null;

function ensureAudio() {
  if (audioCtx) return;
  try {
    audioCtx = new AudioContext();
  } catch {
    audioCtx = null;
  }
}

for (const ev of ["pointerdown", "keydown"]) {
  window.addEventListener(ev, () => ensureAudio(), { once: true, passive: true });
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

export function abortSound() {
  if (!audioCtx || audioCtx.state !== "running") return;
  const t = audioCtx.currentTime;
  for (let i = 0; i < 3; i++) beep(t + i * 0.22, 880);
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
