import { create } from "zustand";
import type {
  ChannelInfo,
  DashboardConfig,
  EventRecord,
  NodeStatus,
  PanelConfig,
  RunMeta,
  SeqStatusMsg,
} from "./types";

export type ViewMode = { mode: "live" } | { mode: "run"; run: RunMeta };

interface TbctlStore {
  status: NodeStatus | null;
  channels: ChannelInfo[];
  seqStatus: SeqStatusMsg | null;
  wsConnected: boolean;
  lastSeqStatusAt: number; // ms epoch of last WS seq_status (heartbeat health)
  dashboard: DashboardConfig | null;
  dashboards: string[];
  editMode: boolean;
  sidebarOpen: boolean;
  runs: RunMeta[];
  viewMode: ViewMode;
  events: EventRecord[];
  view: "dashboard" | "channels" | "sequences";

  setView: (v: "dashboard" | "channels" | "sequences") => void;
  pushEvent: (e: EventRecord) => void;
  setEvents: (e: EventRecord[]) => void;
  setRuns: (r: RunMeta[]) => void;
  setViewMode: (v: ViewMode) => void;
  setStatus: (s: NodeStatus) => void;
  setChannels: (c: ChannelInfo[]) => void;
  setSeqStatus: (s: SeqStatusMsg) => void;
  setWsConnected: (b: boolean) => void;
  setDashboard: (d: DashboardConfig) => void;
  setDashboards: (names: string[]) => void;
  setEditMode: (b: boolean) => void;
  toggleSidebar: () => void;
  updatePanelLayout: (id: string, x: number, y: number, w: number, h: number) => void;
  updatePanelChannels: (id: string, channels: string[]) => void;
  addPanel: (p: PanelConfig) => void;
  removePanel: (id: string) => void;
  updatePanel: (id: string, patch: Partial<PanelConfig>) => void;
}

export const useStore = create<TbctlStore>((set) => ({
  status: null,
  channels: [],
  seqStatus: null,
  wsConnected: false,
  lastSeqStatusAt: 0,
  dashboard: null,
  dashboards: [],
  editMode: false,
  sidebarOpen: true,
  runs: [],
  viewMode: { mode: "live" },
  events: [],
  view: "dashboard",

  setView: (view) => set({ view }),
  pushEvent: (e) => set((s) => ({ events: [e, ...s.events].slice(0, 200) })),
  setEvents: (events) => set({ events: events.slice(0, 200) }),
  setRuns: (runs) => set({ runs }),
  setViewMode: (viewMode) => set({ viewMode }),
  setStatus: (status) => set({ status }),
  setChannels: (channels) => set({ channels }),
  setSeqStatus: (seqStatus) => set({ seqStatus, lastSeqStatusAt: Date.now() }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
  setDashboard: (dashboard) => set({ dashboard }),
  setDashboards: (dashboards) => set({ dashboards }),
  setEditMode: (editMode) => set({ editMode }),
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  updatePanelLayout: (id, x, y, w, h) =>
    set((s) => {
      if (!s.dashboard) return s;
      return {
        dashboard: {
          ...s.dashboard,
          panels: s.dashboard.panels.map((p) => (p.id === id ? { ...p, x, y, w, h } : p)),
        },
      };
    }),
  updatePanelChannels: (id, channels) =>
    set((s) => {
      if (!s.dashboard) return s;
      return {
        dashboard: {
          ...s.dashboard,
          panels: s.dashboard.panels.map((p) => (p.id === id ? { ...p, channels } : p)),
        },
      };
    }),
  addPanel: (p) =>
    set((s) =>
      s.dashboard ? { dashboard: { ...s.dashboard, panels: [...s.dashboard.panels, p] } } : s,
    ),
  removePanel: (id) =>
    set((s) =>
      s.dashboard
        ? { dashboard: { ...s.dashboard, panels: s.dashboard.panels.filter((p) => p.id !== id) } }
        : s,
    ),
  updatePanel: (id, patch) =>
    set((s) =>
      s.dashboard
        ? {
            dashboard: {
              ...s.dashboard,
              panels: s.dashboard.panels.map((p) => (p.id === id ? { ...p, ...patch } : p)),
            },
          }
        : s,
    ),
}));
