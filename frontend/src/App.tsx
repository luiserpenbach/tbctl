import { useEffect } from "react";
import { fetchChannels, fetchDashboard, fetchDashboards, fetchStatus } from "./api";
import { AlarmBanner } from "./components/AlarmBanner";
import { ChannelsEditor } from "./components/ChannelsEditor";
import { DashboardGrid } from "./components/DashboardGrid";
import { DashboardTabs } from "./components/DashboardTabs";
import { SequencesEditor } from "./components/SequencesEditor";
import { Sidebar } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { useStore } from "./store";
import { connectWs } from "./ws";

export default function App() {
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const view = useStore((s) => s.view);

  useEffect(() => {
    connectWs();
    fetchChannels().then((r) => useStore.getState().setChannels(r.channels));
    fetchDashboards().then(async (r) => {
      useStore.getState().setDashboards(r.dashboards);
      const last = localStorage.getItem("tbctl.dashboard");
      const pick = last && r.dashboards.includes(last) ? last : r.dashboards[0];
      if (pick) useStore.getState().setDashboard(await fetchDashboard(pick));
    });
    const poll = () => fetchStatus().then((s) => useStore.getState().setStatus(s)).catch(() => {});
    poll();
    const iv = setInterval(poll, 2000);
    return () => clearInterval(iv);
  }, []);

  return (
    <div className="app">
      <StatusBar />
      <AlarmBanner />
      <div className="app-body">
        {sidebarOpen && <Sidebar />}
        <main className="app-main">
          {view === "dashboard" && (
            <>
              <DashboardTabs />
              <DashboardGrid />
            </>
          )}
          {view === "channels" && <ChannelsEditor />}
          {view === "sequences" && <SequencesEditor />}
        </main>
      </div>
    </div>
  );
}
