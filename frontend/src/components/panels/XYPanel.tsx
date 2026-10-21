import { useEffect, useRef, useState } from "react";
import uPlot from "uplot";
import { fetchRunData } from "../../api";
import { seriesColor, token } from "../../colors";
import { buffers, onTick } from "../../databuf";
import { useStore } from "../../store";
import type { PanelConfig } from "../../types";

// XY / scatter panel (tier 2): plot one channel against another — thrust vs
// chamber pressure, ΔP vs flow — for steady-state characterization. Custom
// canvas (uPlot's mode-1 needs ascending x, which XY data isn't). Live mode
// shows the recent window with older points fading out; run mode shows the
// whole recording.

export function XYPanel({ panel }: { panel: PanelConfig }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewMode = useStore((s) => s.viewMode);
  const channels = useStore((s) => s.channels);
  const [runPts, setRunPts] = useState<[number[], number[]] | null>(null);
  const xTag = panel.channels[0];
  const yTag = panel.channels[1];
  const isHistory = viewMode.mode === "run";
  const windowS = panel.window_s ?? 60;

  const unitOf = (tag?: string) => channels.find((c) => c.tag === tag)?.unit ?? "";

  useEffect(() => {
    setRunPts(null);
    if (isHistory && viewMode.mode === "run" && xTag && yTag) {
      fetchRunData(viewMode.run.run_id, [xTag, yTag], 4000)
        .then((r) => {
          const sx = r.series[xTag];
          const sy = r.series[yTag];
          if (!sx?.t.length || !sy?.t.length) return;
          const joined = uPlot.join([
            [sx.t, sx.mean] as uPlot.AlignedData,
            [sy.t, sy.mean] as uPlot.AlignedData,
          ]);
          const xs: number[] = [];
          const ys: number[] = [];
          const [, jx, jy] = joined as unknown as [number[], number[], number[]];
          for (let i = 0; i < jx.length; i++) {
            if (jx[i] != null && jy[i] != null) {
              xs.push(jx[i]);
              ys.push(jy[i]);
            }
          }
          setRunPts([xs, ys]);
        })
        .catch(() => {});
    }
  }, [isHistory, viewMode.mode === "run" ? viewMode.run.run_id : "", xTag, yTag]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !xTag || !yTag) return;

    const draw = () => {
      const dpr = devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (w === 0 || h === 0) return;
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      const ctx = canvas.getContext("2d")!;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      let xs: number[] = [];
      let ys: number[] = [];
      if (isHistory) {
        if (runPts) [xs, ys] = runPts;
      } else {
        const data = buffers.aligned([xTag, yTag]) as unknown as [
          number[],
          (number | null)[],
          (number | null)[],
        ];
        const [t, jx, jy] = data;
        const cutoff = (buffers.latestTime() ?? 0) - windowS;
        for (let i = 0; i < t.length; i++) {
          if (t[i] >= cutoff && jx[i] != null && jy[i] != null) {
            xs.push(jx[i] as number);
            ys.push(jy[i] as number);
          }
        }
      }

      const pad = 44;
      const padT = 10;
      const padR = 10;
      const padB = 26;
      const plotW = w - pad - padR;
      const plotH = h - padT - padB;
      const faint = token("--subtle");
      ctx.strokeStyle = token("--line");
      ctx.lineWidth = 1;
      ctx.strokeRect(pad, padT, plotW, plotH);

      ctx.font = "10.5px 'Geist Mono Variable', monospace";
      ctx.fillStyle = faint;
      if (xs.length < 2) {
        ctx.textAlign = "center";
        ctx.fillText("waiting for data…", w / 2, h / 2);
        return;
      }

      let xmin = Math.min(...xs);
      let xmax = Math.max(...xs);
      let ymin = Math.min(...ys);
      let ymax = Math.max(...ys);
      const xm = (xmax - xmin) * 0.05 || 0.5;
      const ym = (ymax - ymin) * 0.05 || 0.5;
      xmin -= xm;
      xmax += xm;
      ymin -= ym;
      ymax += ym;
      const sx = (v: number) => pad + ((v - xmin) / (xmax - xmin)) * plotW;
      const sy = (v: number) => padT + plotH - ((v - ymin) / (ymax - ymin)) * plotH;

      // axis labels: min/max + channel names
      ctx.fillStyle = token("--plot-axis");
      ctx.textAlign = "left";
      ctx.fillText(ymax.toFixed(1), 4, padT + 8);
      ctx.fillText(ymin.toFixed(1), 4, padT + plotH);
      ctx.textAlign = "center";
      ctx.fillText(xmin.toFixed(1), pad, h - 12);
      ctx.fillText(xmax.toFixed(1), pad + plotW, h - 12);
      ctx.fillStyle = faint;
      ctx.fillText(`${xTag} [${unitOf(xTag)}]`, pad + plotW / 2, h - 2);
      ctx.save();
      ctx.translate(10, padT + plotH / 2);
      ctx.rotate(-Math.PI / 2);
      ctx.fillText(`${yTag} [${unitOf(yTag)}]`, 0, 0);
      ctx.restore();

      // points: newest brightest (live), uniform in run mode
      ctx.fillStyle = seriesColor(0);
      for (let i = 0; i < xs.length; i++) {
        const age = isHistory ? 0.35 : 1 - i / xs.length;
        ctx.globalAlpha = Math.max(0.15, 0.9 - age * 0.75);
        ctx.fillRect(sx(xs[i]) - 1.25, sy(ys[i]) - 1.25, 2.5, 2.5);
      }
      ctx.globalAlpha = 1;
    };

    draw();
    const off = isHistory ? () => {} : onTick(draw);
    const ro = new ResizeObserver(draw);
    ro.observe(canvas);
    return () => {
      off();
      ro.disconnect();
    };
  }, [xTag, yTag, isHistory, runPts, windowS, channels]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!xTag || !yTag)
    return <div className="placeholder">configure X and Y channels (panel settings)</div>;
  return <canvas ref={canvasRef} style={{ width: "100%", height: "100%", display: "block" }} />;
}
