import { fetchDashboard, fetchDashboards, post, saveDashboard } from "../api";
import { useStore } from "../store";

async function del(url: string) {
  const r = await fetch(url, { method: "DELETE" });
  if (!r.ok) throw new Error(`${url}: ${r.status} ${await r.text()}`);
  return r.json();
}

export function DashboardTabs() {
  const dashboards = useStore((s) => s.dashboards);
  const dashboard = useStore((s) => s.dashboard);
  const setDashboard = useStore((s) => s.setDashboard);
  const setDashboards = useStore((s) => s.setDashboards);
  const editMode = useStore((s) => s.editMode);

  const open = async (name: string) => {
    setDashboard(await fetchDashboard(name));
    localStorage.setItem("tbctl.dashboard", name);
  };

  const create = async (copyFrom?: string) => {
    const name = prompt(copyFrom ? `duplicate ${copyFrom} as:` : "new dashboard name:");
    if (!name || !/^[a-zA-Z0-9_-]+$/.test(name)) {
      if (name) alert("name must be [a-zA-Z0-9_-]+");
      return;
    }
    const base = copyFrom
      ? await fetchDashboard(copyFrom)
      : { name, grid: { cols: 12, row_height_px: 40 }, panels: [] };
    await post(`/api/dashboards/${name}`, { ...base, name });
    setDashboards((await fetchDashboards()).dashboards);
    await open(name);
  };

  const remove = async (name: string) => {
    if (!confirm(`delete dashboard ${name}?`)) return;
    try {
      await del(`/api/dashboards/${name}`);
    } catch (e) {
      alert(String(e));
      return;
    }
    const rest = (await fetchDashboards()).dashboards;
    setDashboards(rest);
    if (dashboard?.name === name && rest.length) await open(rest[0]);
  };

  return (
    <div className="dash-tabs">
      {dashboards.map((name) => (
        <span
          key={name}
          className={`dash-tab ${dashboard?.name === name ? "active" : ""}`}
          onClick={() => open(name)}
        >
          {name}
          {editMode && dashboards.length > 1 && (
            <button
              className="tab-x"
              title="delete dashboard"
              onClick={(e) => {
                e.stopPropagation();
                remove(name);
              }}
            >
              ×
            </button>
          )}
        </span>
      ))}
      {editMode && (
        <>
          <button className="btn" title="new dashboard" onClick={() => create()}>
            +
          </button>
          {dashboard && (
            <button className="btn" title="duplicate current" onClick={() => create(dashboard.name)}>
              ⧉
            </button>
          )}
          {dashboard && (
            <button
              className="btn"
              title="save current dashboard"
              onClick={() =>
                saveDashboard(dashboard.name, dashboard).catch((e) => alert(String(e)))
              }
            >
              Save
            </button>
          )}
        </>
      )}
    </div>
  );
}
