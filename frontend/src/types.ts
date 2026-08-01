// API payload types, mirroring the tbctl-node REST/WS contracts.

export interface ChannelInfo {
  tag: string;
  hw: string;
  kind: "analog_in" | "thermocouple_k" | "valve" | "spark";
  unit?: string;
  rate_group?: string;
  rate_hz?: number;
  fail_safe?: string;
  bit?: number;
  active_level?: number;
}

export interface NodeStatus {
  time: number;
  device: { connected: boolean; ip: string; sim: boolean };
  sequencer: {
    state: string;
    error: string;
    cur_step: number;
    elapsed_ms: number;
    interp_version: number;
  };
  acquisition: { running: boolean; scan_rate_hz: number; sample_count: number };
  recording: { active: boolean; run_id: string | null; t_start: number | null };
  control_enabled: boolean;
  outputs: Record<string, string>;
  config: Record<string, unknown>;
}

export interface SeqStatusMsg {
  type: "seq_status";
  time: number;
  state: string;
  error: string;
  cur_step: number;
  elapsed_ms: number;
  step_count: number;
  crc16: number;
  dio_state: number;
}

export interface FrameMsg {
  type: "frame";
  t0: number;
  dt: number;
  t: number[];
  tags: string[];
  data: number[][];
}

export interface EventMsg {
  type: "event";
  event: string;
  time: number;
  [k: string]: unknown;
}

export type WsMsg = FrameMsg | SeqStatusMsg | EventMsg;

export interface PanelConfig {
  id: string;
  type: "timeseries" | "value_table" | "valve_panel" | "sequence_panel" | "recorder_panel";
  x: number;
  y: number;
  w: number;
  h: number;
  channels: string[];
  y_axis: { mode: "auto" | "manual"; min: number | null; max: number | null };
}

export interface RunMeta {
  run_id: string;
  name: string;
  operator: string;
  notes: string;
  t_start: number;
  t_stop: number | null;
  sample_counts: Record<string, number>;
  size_bytes?: number;
}

export interface DashboardConfig {
  name: string;
  grid: { cols: number; row_height_px: number };
  panels: PanelConfig[];
}
