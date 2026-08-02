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
