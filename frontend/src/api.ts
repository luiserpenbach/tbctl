import type { ChannelInfo, DashboardConfig, NodeStatus } from "./types";

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
