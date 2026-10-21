import { useMemo, useRef, useState } from "react";
import GridLayout, { WidthProvider, type Layout } from "react-grid-layout";
import { useStore } from "../store";
import type { PanelConfig } from "../types";
import { PanelModal } from "./PanelModal";
import { ChecklistPanel } from "./panels/ChecklistPanel";
import { PidPanel } from "./panels/PidPanel";
import { RawTablePanel } from "./panels/RawTablePanel";
import { RecorderPanel } from "./panels/RecorderPanel";
import { SequencePanel } from "./panels/SequencePanel";
import { SimPanel } from "./panels/SimPanel";
import { TimeseriesPanel } from "./panels/TimeseriesPanel";
import { ValueTablePanel } from "./panels/ValueTablePanel";
import { ValvePanel } from "./panels/ValvePanel";
import { XYPanel } from "./panels/XYPanel";

const Grid = WidthProvider(GridLayout);

export const PANEL_TITLES: Record<PanelConfig["type"], string> = {
  timeseries: "Time Series",
  value_table: "Live Values",
  valve_panel: "Valve Control",
  sequence_panel: "Sequence",
  recorder_panel: "Recorder",
  raw_table: "Raw Channels",
  sim_panel: "Simulator",
  pid: "P&ID",
  checklist_panel: "Checklist",
  xy: "XY Plot",
};

function PanelBody({ panel }: { panel: PanelConfig }) {
  switch (panel.type) {
    case "timeseries":
      return <TimeseriesPanel panel={panel} />;
    case "value_table":
      return <ValueTablePanel panel={panel} />;
    case "valve_panel":
      return <ValvePanel />;
    case "sequence_panel":
      return <SequencePanel />;
    case "recorder_panel":
      return <RecorderPanel />;
    case "raw_table":
      return <RawTablePanel />;
    case "sim_panel":
      return <SimPanel />;
    case "pid":
      return <PidPanel panel={panel} />;
    case "checklist_panel":
      return <ChecklistPanel />;
    case "xy":
      return <XYPanel panel={panel} />;
  }
}

export function DashboardGrid() {
  const dashboard = useStore((s) => s.dashboard);
  const editMode = useStore((s) => s.editMode);
  const updatePanelLayout = useStore((s) => s.updatePanelLayout);
  const removePanel = useStore((s) => s.removePanel);
  // null = closed; "new" = add-panel; PanelConfig = configure existing
  const [modal, setModal] = useState<"new" | PanelConfig | null>(null);
  // avoid feedback loops: RGL fires onLayoutChange on mount too
  const dragging = useRef(false);

  const layout: Layout[] = useMemo(
    () =>
      (dashboard?.panels ?? []).map((p) => ({
        i: p.id,
        x: p.x,
        y: p.y,
        w: p.w,
        h: p.h,
        minW: 2,
        minH: 2,
      })),
    [dashboard],
  );

  if (!dashboard) return <div className="placeholder">no dashboard loaded</div>;

  const commit = (l: Layout[]) => {
    for (const item of l) updatePanelLayout(item.i, item.x, item.y, item.w, item.h);
  };

  return (
    <>
    {editMode && (
      <div className="edit-toolbar">
        <button className="btn" onClick={() => setModal("new")}>
          + Add panel
        </button>
        <span style={{ fontSize: 11, color: "var(--text-faint)" }}>
          Drag headers to move · drag corners to resize · Save in the top bar
        </span>
      </div>
    )}
    <Grid
      className={`layout ${editMode ? "editing" : ""}`}
      layout={layout}
      cols={dashboard.grid.cols}
      rowHeight={dashboard.grid.row_height_px}
      margin={[8, 8]}
      isDraggable={editMode}
      isResizable={editMode}
      draggableHandle=".drag-handle"
      onDragStart={() => (dragging.current = true)}
      onResizeStart={() => (dragging.current = true)}
      onDragStop={(l) => {
        dragging.current = false;
        commit(l);
      }}
      onResizeStop={(l) => {
        dragging.current = false;
        commit(l);
      }}
    >
      {dashboard.panels.map((p) => (
        <div key={p.id}>
          <div className="panel">
            <div className="panel-header">
              <span className="drag-handle">{p.title || PANEL_TITLES[p.type] || p.type}</span>
              <span>{p.id.toUpperCase()}</span>
              {editMode && (
                <>
                  <button className="panel-btn" title="configure" onClick={() => setModal(p)}>
                    ⚙
                  </button>
                  <button className="panel-btn" title="remove" onClick={() => removePanel(p.id)}>
                    ✕
                  </button>
                </>
              )}
            </div>
            <div className="panel-body">
              <PanelBody panel={p} />
            </div>
          </div>
        </div>
      ))}
    </Grid>
    {modal !== null && (
      <PanelModal panel={modal === "new" ? null : modal} onClose={() => setModal(null)} />
    )}
    </>
  );
}
