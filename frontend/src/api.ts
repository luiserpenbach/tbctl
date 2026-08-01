import type { ChannelInfo, DashboardConfig, NodeStatus, RunMeta } from "./types";

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
