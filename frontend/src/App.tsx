import { useCallback, useEffect, useRef, useState } from "react";
import { fetchChannels, fetchDashboard, fetchDashboards, fetchStatus } from "./api";
import { AlarmBanner } from "./components/AlarmBanner";
import { ChannelsEditor } from "./components/ChannelsEditor";
import { DashboardGrid } from "./components/DashboardGrid";
import { DashboardTabs } from "./components/DashboardTabs";
import { PidEditor } from "./components/PidEditor";
import { RunAnnotations } from "./components/RunAnnotations";
import { RunEventsBar } from "./components/RunEventsBar";
import { RunReportBar } from "./components/RunReportBar";
import { SequencesEditor } from "./components/SequencesEditor";
import { Sidebar } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { useStore } from "./store";
import { applyHashOnce, startHashSync } from "./urlstate";
import { connectWs } from "./ws";

export default function App() {
  const sidebarOpen = useStore((s) => s.sidebarOpen);
  const view = useStore((s) => s.view);
  // M26: a transient 502 at page load must not leave an empty console until
  // hard refresh — show the failure and retry with backoff
  const [initError, setInitError] = useState<string | null>(null);
  const initRef = useRef<{ attempt: number; timer: ReturnType<typeof setTimeout> | null; cancelled: boolean }>({
    attempt: 0,
    timer: null,
    cancelled: false,
  });

  const loadInitial = useCallback(async () => {
    const st = initRef.current;
    if (st.timer) {
      clearTimeout(st.timer);
      st.timer = null;
    }
    try {
      const [ch, db] = await Promise.all([fetchChannels(), fetchDashboards()]);
      if (st.cancelled) return;
      useStore.getState().setChannels(ch.channels);
      useStore.getState().setDashboards(db.dashboards);
      const last = localStorage.getItem("tbctl.dashboard");
      const pick = last && db.dashboards.includes(last) ? last : db.dashboards[0];
      if (pick) useStore.getState().setDashboard(await fetchDashboard(pick));
      await applyHashOnce(); // shareable link state wins over defaults
      st.attempt = 0;
      setInitError(null);
    } catch (e) {
      if (st.cancelled) return;
      const delay = Math.min(2000 * 2 ** st.attempt, 15000);
      st.attempt += 1;
      setInitError(
        `${String(e).replace(/^Error: /, "")} — retrying in ${Math.round(delay / 1000)} s`,
      );
      st.timer = setTimeout(loadInitial, delay);
    }
  }, []);

  useEffect(() => {
    connectWs();
    initRef.current.cancelled = false;
    loadInitial();
    const stopSync = startHashSync();
    // status poll: in-flight + out-of-order guards so a slow response can't
    // overwrite a fresher one
    let statusEpoch = 0;
    let statusInFlight = false;
    const poll = () => {
      if (statusInFlight) return;
      const n = ++statusEpoch;
      statusInFlight = true;
      fetchStatus()
        .then((s) => {
          if (n === statusEpoch) useStore.getState().setStatus(s);
        })
        .catch(() => {})
        .finally(() => {
          statusInFlight = false;
        });
    };
    poll();
    const iv = setInterval(poll, 2000);
    return () => {
      clearInterval(iv);
      stopSync();
      initRef.current.cancelled = true;
      if (initRef.current.timer) clearTimeout(initRef.current.timer);
    };
  }, [loadInitial]);

  return (
    <div className="app">
      <StatusBar />
      <AlarmBanner />
      {initError && (
        <div className="init-error-banner">
          <span>startup load failed: {initError}</span>
          <button
            className="btn"
            onClick={() => {
              initRef.current.attempt = 0;
              loadInitial();
            }}
          >
            RETRY NOW
          </button>
        </div>
      )}
      <div className="app-body">
        {sidebarOpen && <Sidebar />}
        <main className="app-main">
          {view === "dashboard" && (
            <>
              <DashboardTabs />
              <RunReportBar />
              <RunAnnotations />
              <RunEventsBar />
              <DashboardGrid />
            </>
          )}
          {view === "channels" && <ChannelsEditor />}
          {view === "sequences" && <SequencesEditor />}
          {view === "pid" && <PidEditor />}
        </main>
      </div>
    </div>
  );
}
