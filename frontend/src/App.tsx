import { useEffect } from "react";
import { fetchChannels, fetchDashboard, fetchDashboards, fetchStatus } from "./api";
import { AlarmBanner } from "./components/AlarmBanner";
import { DashboardGrid } from "./components/DashboardGrid";
import { Sidebar } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { useStore } from "./store";
import { connectWs } from "./ws";

export default function App() {
  const sidebarOpen = useStore((s) => s.sidebarOpen);

  useEffect(() => {
    connectWs();
    fetchChannels().then((r) => useStore.getState().setChannels(r.channels));
    fetchDashboards().then(async (r) => {
      useStore.getState().setDashboards(r.dashboards);
      if (r.dashboards.length) {
        useStore.getState().setDashboard(await fetchDashboard(r.dashboards[0]));
      }
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
          <DashboardGrid />
        </main>
      </div>
    </div>
  );
}
