import { useMemo, useRef } from "react";
import GridLayout, { WidthProvider, type Layout } from "react-grid-layout";
import { useStore } from "../store";
import type { PanelConfig } from "../types";
import { RecorderPanel } from "./panels/RecorderPanel";
import { SequencePanel } from "./panels/SequencePanel";
import { TimeseriesPanel } from "./panels/TimeseriesPanel";
import { ValueTablePanel } from "./panels/ValueTablePanel";
import { ValvePanel } from "./panels/ValvePanel";

const Grid = WidthProvider(GridLayout);

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
  }
}

export function DashboardGrid() {
  const dashboard = useStore((s) => s.dashboard);
  const editMode = useStore((s) => s.editMode);
  const updatePanelLayout = useStore((s) => s.updatePanelLayout);
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
    <Grid
      className="layout"
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
              <span className="drag-handle">{p.type.replace("_", " ")}</span>
              <span>{p.id}</span>
            </div>
            <div className="panel-body">
              <PanelBody panel={p} />
            </div>
          </div>
        </div>
      ))}
    </Grid>
  );
}
