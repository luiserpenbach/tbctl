import type { ChannelInfo, DashboardConfig, EventRecord, NodeStatus, RunMeta } from "./types";

async function get<T>(url: string): Promise<T> {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

export async function post<T>(url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

export const fetchStatus = () => get<NodeStatus>("/api/status");
export const fetchChannels = () =>
  get<{ channels: ChannelInfo[]; rate_groups: Record<string, number> }>("/api/channels");
export const fetchDashboards = () => get<{ dashboards: string[] }>("/api/dashboards");
export const fetchDashboard = (name: string) => get<DashboardConfig>(`/api/dashboards/${name}`);

export interface LiveSeries {
  t: number[];
  mean: number[];
  min: number[];
  max: number[];
}

export const fetchLive = (tags: string[], windowS: number, maxPoints = 2000) =>
  get<{ window_s: number; series: Record<string, LiveSeries> }>(
    `/api/live?tags=${tags.join(",")}&window_s=${windowS}&max_points=${maxPoints}`,
  );

export const fetchRuns = () => get<{ runs: RunMeta[] }>("/api/runs");
export const fetchRunData = (runId: string, tags: string[], maxPoints = 2000) =>
  get<{ run_id: string; series: Record<string, LiveSeries> }>(
    `/api/runs/${runId}/data?tags=${tags.join(",")}&max_points=${maxPoints}`,
  );
export const startRun = (name: string, operator: string, notes: string) =>
  post<RunMeta>("/api/runs/start", { name, operator, notes });
export const stopRun = () => post<RunMeta>("/api/runs/stop");
export const saveDashboard = (name: string, body: DashboardConfig) =>
  post<{ ok: boolean; path: string }>(`/api/dashboards/${name}`, body);

export interface RedlineInfo {
  tag: string;
  min: number | null;
  max: number | null;
  persist_ms: number;
}

export interface SequenceInfo {
  name: string;
  description: string;
  n_steps: number;
  n_abort_steps: number;
  max_duration_s: number;
  allow_unsafe_end: boolean;
  redlines: RedlineInfo[];
  sha256: string;
  loaded: boolean;
}

export interface CompileReport {
  name: string;
  n_steps: number;
  n_abort_steps: number;
  duration_ms: number;
  abort_duration_ms: number;
  crc16: number;
  crc_match: boolean;
  warnings: string[];
  redlines: RedlineInfo[];
}

export interface SeqStepCfg {
  t: number;
  set: Record<string, string>;
}

export interface BoundCfg {
  tag: string;
  min?: number;
  max?: number;
  persist_ms?: number;
}

export interface SequenceConfig {
  name: string;
  description: string;
  max_duration_s: number;
  preconditions: BoundCfg[];
  steps: SeqStepCfg[];
  abort: SeqStepCfg[];
  allow_unsafe_end: boolean;
  redlines: BoundCfg[];
}

export interface TimelineData {
  outputs: { tag: string; bit: number; kind: string; active_level: number }[];
  safe_word: number;
  duration_ms: number;
  abort_duration_ms: number;
  steps: { t_ms: number; word: number }[];
  abort: { t_ms: number; word: number }[];
}

export interface ValidateOk {
  ok: true;
  report: CompileReport & { size_bytes: number; safe_word: number };
  timeline: TimelineData;
}

export interface ValidateErr {
  ok: false;
  errors: { loc: string; msg: string }[];
}

export const fetchSequenceConfig = (name: string) =>
  get<{ config: SequenceConfig; sha256: string; editable: boolean }>(
    `/api/sequences/${name}/config`,
  );

export async function validateSequence(body: SequenceConfig): Promise<ValidateOk | ValidateErr> {
  const r = await fetch("/api/sequences/validate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (r.ok) return { ok: true, ...(await r.json()) };
  const d = await r.json().catch(() => null);
  const detail = d?.detail;
  const errors = Array.isArray(detail)
    ? detail
    : [{ loc: "", msg: String(detail ?? `HTTP ${r.status}`) }];
  return { ok: false, errors };
}

export const saveSequence = (name: string, body: SequenceConfig) =>
  post<{ ok: boolean; sha256: string; backup: string | null }>(`/api/sequences/${name}`, body);

export const fetchSequences = () => get<{ sequences: SequenceInfo[] }>("/api/sequences");
export const loadSequence = (name: string) => post<CompileReport>(`/api/sequences/${name}/load`);
export const armSequence = (confirm: string) => post("/api/sequences/arm", { confirm });
export const fireSequence = (confirm: string) => post("/api/sequences/fire", { confirm });
export const abortSequence = () => post("/api/sequences/abort");
export const resetSequence = () => post("/api/sequences/reset");
export const setManualValve = (tag: string, state: string) =>
  post("/api/manual/valve", { tag, state });
export const setControlMode = (enabled: boolean) => post("/api/control-mode", { enabled });
export const fetchEvents = (limit = 100) =>
  get<{ events: EventRecord[] }>(`/api/events?limit=${limit}`);

// ----------------------------------------------------------- extras (ph. 15)

export const postRunNote = (text: string) => post("/api/runs/note", { text });
export const fetchRunEvents = (runId: string) =>
  get<{ events: EventRecord[] }>(`/api/runs/${runId}/events`);
export const runCsvUrl = (runId: string, tags: string[]) =>
  `/api/runs/${runId}/export.csv?tags=${encodeURIComponent(tags.join(","))}`;

export interface ChecklistState {
  name: string;
  items: string[];
  checked: boolean[];
}

export const fetchChecklists = () =>
  get<{ checklists: { name: string; items: string[] }[]; active: ChecklistState | null }>(
    "/api/checklists",
  );
export const activateChecklist = (name: string | null) =>
  post<{ active: ChecklistState | null }>("/api/checklists/activate", { name });
export const checkChecklistItem = (index: number, checked: boolean) =>
  post<{ active: ChecklistState | null }>("/api/checklists/check", { index, checked });

// ------------------------------------------------------ run reports (tier 1)

export interface ReviewResult {
  name: string;
  tag: string;
  metric: string;
  window: string;
  limit_min: number | null;
  limit_max: number | null;
  value: number | null;
  passed: boolean | null;
  skipped: string | null;
}

export interface RunReport {
  generated_at: number;
  sequence: string | null;
  fire_window: [number, number | null] | null;
  results: ReviewResult[];
  summary: { total: number; passed: number; failed: number; skipped: number };
}

export const fetchRunReport = (runId: string) =>
  get<{ report: RunReport | null }>(`/api/runs/${runId}/report`);
export const rerunRunReport = (runId: string) =>
  post<{ report: RunReport }>(`/api/runs/${runId}/report/rerun`);

// ------------------------------------------------------ run meta (tier 2)

export const patchRunMeta = async (
  runId: string,
  body: { tags?: string[]; outcome?: "good" | "anomaly" | "scrub"; clear_outcome?: boolean },
) => {
  const r = await fetch(`/api/runs/${runId}/meta`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};

// -------------------------------------------------------- annotations (tier 1)

export interface Annotation {
  id: string;
  t0: number;
  t1: number;
  label: string;
  severity: "info" | "warn" | "crit";
  status: "open" | "resolved";
  source: "user" | "rule";
  created_at: number;
  updated_at: number;
}

export const fetchAnnotations = (runId: string) =>
  get<{ annotations: Annotation[] }>(`/api/runs/${runId}/annotations`);
export const createAnnotation = (
  runId: string,
  body: { t0: number; t1: number; label: string; severity?: Annotation["severity"] },
) => post<{ annotation: Annotation }>(`/api/runs/${runId}/annotations`, body);
export const patchAnnotation = async (
  runId: string,
  aid: string,
  body: Partial<Pick<Annotation, "label" | "severity" | "status">>,
) => {
  const r = await fetch(`/api/runs/${runId}/annotations/${aid}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<{ annotation: Annotation }>;
};
export const deleteAnnotation = async (runId: string, aid: string) => {
  const r = await fetch(`/api/runs/${runId}/annotations/${aid}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};

// ------------------------------------------------------------- P&ID (ph. 13)

export type PidElement =
  | {
      id: string;
      type: "valve";
      channel: string;
      x: number;
      y: number;
      size?: number;
      rotation?: number;
    }
  | {
      id: string;
      type: "sensor";
      channel: string;
      x: number;
      y: number;
      decimals?: number;
      warn_min?: number;
      warn_max?: number;
    }
  | { id: string; type: "label"; text: string; x: number; y: number; size?: number };

export interface PidConfigT {
  name: string;
  background?: string | null;
  elements: PidElement[];
}

export interface PidInfo {
  name: string;
  background: string | null;
  n_elements: number;
}

export const fetchPids = () => get<{ pids: PidInfo[] }>("/api/pid");
export const fetchPid = (name: string) =>
  get<{ config: PidConfigT; sha256: string }>(`/api/pid/${name}`);
export const savePid = (name: string, body: PidConfigT) =>
  post<{ ok: boolean; backup: string | null }>(`/api/pid/${name}`, body);
export const deletePid = async (name: string) => {
  const r = await fetch(`/api/pid/${name}`, { method: "DELETE" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
};
export const fetchPidAssets = () => get<{ assets: string[] }>("/api/pid/assets");
export const uploadPidAsset = (name: string, svg: string) =>
  post<{ ok: boolean; asset: string }>("/api/pid/assets", { name, svg });
