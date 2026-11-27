import { useEffect, useRef, useState } from "react";
import type { PidElement, PidConfigT } from "../../api";

// Shared P&ID rendering surface (roadmap phase 13): background SVG in an
// aspect-preserving content box, elements absolutely positioned at
// normalized 0-1 coordinates against that box. The viewer panel passes live
// state accessors; the editor passes a PidEdit instead and the stage runs the
// pointer gestures (select, marquee, move, alt-drag duplicate, resize).

export interface StageBox {
  left: number;
  top: number;
  w: number;
  h: number;
}

export interface PidEdit {
  selected: ReadonlySet<string>;
  snap: boolean; // snap moves to the grid
  // last pointer position over the stage, normalized; null when outside
  cursor: { current: [number, number] | null };
  onSelect: (ids: string[]) => void;
  checkpoint: () => void; // called once before a gesture changes anything
  patch: (patches: Record<string, { x?: number; y?: number; size?: number }>) => void;
  duplicate: (ids: string[]) => string[]; // returns the copies' ids, same order
  // background mode: drags move / resize the background instead of elements
  bgMode?: boolean;
  bgFollow?: boolean; // elements keep their place on the drawing
  patchBg?: (bg: PidBg, els: Record<string, { x: number; y: number; size?: number }>) => void;
}

// placement of the background image in the content box (fractions of it)
export interface PidBg {
  scale: number;
  x: number;
  y: number;
}
export const bgOf = (c: PidConfigT): PidBg => ({
  scale: c.background_scale ?? 1,
  x: c.background_x ?? 0,
  y: c.background_y ?? 0,
});
export const clampBg = (b: PidBg): PidBg => ({
  scale: +Math.min(10, Math.max(0.05, b.scale)).toFixed(4),
  x: +Math.min(10, Math.max(-10, b.x)).toFixed(4),
  y: +Math.min(10, Math.max(-10, b.y)).toFixed(4),
});
// where a point pinned to the drawing ends up when the background changes
export const followBg = (p: { x: number; y: number }, from: PidBg, to: PidBg) => ({
  x: +Math.min(1, Math.max(0, to.x + ((p.x - from.x) / from.scale) * to.scale)).toFixed(4),
  y: +Math.min(1, Math.max(0, to.y + ((p.y - from.y) / from.scale) * to.scale)).toFixed(4),
});

export const GRID = 0.01; // snap step, fraction of canvas width
const SENSOR_PX = 12; // readout font size when a sensor has no explicit size
const SIZE_MAX = { valve: 0.5, sensor: 0.2, label: 0.2 } as const;
const SIZE_MIN = 0.004;

// an element pinned to the drawing: position follows the background, and so
// does its size (a sensor without an explicit size keeps its fixed readout)
export function followEl(e: PidElement, from: PidBg, to: PidBg) {
  const k = to.scale / from.scale;
  const size = e.type === "sensor" && e.size == null ? null : clampSize(e.type, elSize(e) * k);
  return { ...followBg(e, from, to), ...(size !== null && k !== 1 ? { size } : {}) };
}

export const clampSize = (type: PidElement["type"], v: number) =>
  +Math.min(SIZE_MAX[type], Math.max(SIZE_MIN, v)).toFixed(4);

// effective size of an element as a fraction of canvas width
export function elSize(el: PidElement, boxW = 1000): number {
  if (el.type === "valve") return el.size ?? 0.05;
  if (el.type === "label") return el.size ?? 0.022;
  return el.size ?? SENSOR_PX / (boxW || 1000);
}

export function ValveSymbol({
  kind,
  active,
  px,
  rotation,
  selected,
}: {
  kind: "valve" | "igniter";
  active: boolean | null; // null = unknown
  px: number;
  rotation: number;
  selected?: boolean;
}) {
  const stroke = selected ? "var(--accent)" : active === null ? "var(--text-faint)" : "var(--muted)";
  if (kind === "igniter") {
    // lightning bolt, amber when firing
    return (
      <svg
        width={px}
        height={px}
        viewBox="0 0 24 24"
        style={{ transform: `rotate(${rotation}deg)`, display: "block" }}
      >
        <path
          d="M13 2 L5 14 L11 14 L9 22 L19 9 L13 9 z"
          fill={active ? "var(--amber)" : "none"}
          stroke={active ? "var(--amber)" : stroke}
          strokeWidth="1.6"
          strokeLinejoin="round"
        />
      </svg>
    );
  }
  // bowtie valve: open = green fill, closed = hollow
  return (
    <svg
      width={px}
      height={(px * 24) / 40}
      viewBox="0 0 40 24"
      style={{ transform: `rotate(${rotation}deg)`, display: "block" }}
    >
      <path
        d="M2,2 L20,12 L2,22 z M38,2 L20,12 L38,22 z"
        fill={active ? "var(--green)" : "var(--bg)"}
        stroke={active ? "var(--green)" : stroke}
        strokeWidth="2"
        strokeLinejoin="round"
      />
    </svg>
  );
}

type Gesture =
  | {
      kind: "move";
      primary: string;
      start: [number, number];
      origin: Map<string, [number, number]>;
      moved: boolean;
      dup: boolean; // alt-drag: copy on first movement
    }
  | {
      kind: "resize";
      center: [number, number]; // client px
      d0: number;
      origin: Map<string, number>;
      moved: boolean;
    }
  | { kind: "marquee"; start: [number, number]; base: string[]; moved: boolean }
  | {
      kind: "bg";
      resize: boolean;
      start: [number, number];
      bg0: PidBg;
      d0: number; // px from the image's top-left corner at grab time
      els: PidElement[];
      moved: boolean;
    };

export function PidStage({
  cfg,
  valveKind,
  liveValve,
  liveValue,
  staleOf,
  unitOf,
  onElementClick,
  edit,
  children,
}: {
  cfg: PidConfigT;
  valveKind: (channel: string) => "valve" | "igniter";
  liveValve?: (channel: string) => boolean | null;
  liveValue?: (channel: string) => number | null;
  staleOf?: (channel: string) => boolean;
  unitOf?: (channel: string) => string;
  onElementClick?: (el: PidElement) => void;
  edit?: PidEdit;
  children?: (box: StageBox) => React.ReactNode; // overlay (confirm popover)
}) {
  const outer = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const gesture = useRef<Gesture | null>(null);
  const [marquee, setMarquee] = useState<[number, number, number, number] | null>(null);
  const [guides, setGuides] = useState<{ x?: number; y?: number } | null>(null);

  useEffect(() => {
    const el = outer.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // content box: aspect-fit the background, else fill
  let box: StageBox = { left: 0, top: 0, w: size.w, h: size.h };
  if (cfg.background && natural && natural.w > 0 && size.w > 0 && size.h > 0) {
    const scale = Math.min(size.w / natural.w, size.h / natural.h);
    const w = natural.w * scale;
    const h = natural.h * scale;
    box = { left: (size.w - w) / 2, top: (size.h - h) / 2, w, h };
  }

  // unclamped normalized position of a pointer event
  const toNorm = (ev: { clientX: number; clientY: number }): [number, number] => {
    const r = outer.current!.getBoundingClientRect();
    return [
      (ev.clientX - r.left - box.left) / (box.w || 1),
      (ev.clientY - r.top - box.top) / (box.h || 1),
    ];
  };
  const capture = (ev: React.PointerEvent) => outer.current?.setPointerCapture(ev.pointerId);

  const bg = bgOf(cfg);
  const downBg = (ev: React.PointerEvent, resize: boolean) => {
    if (!edit || ev.button !== 0 || !cfg.background) return;
    ev.stopPropagation();
    const start = toNorm(ev);
    gesture.current = {
      kind: "bg",
      resize,
      start,
      bg0: bg,
      d0: Math.max(8, Math.hypot((start[0] - bg.x) * box.w, (start[1] - bg.y) * box.h)),
      els: cfg.elements,
      moved: false,
    };
    capture(ev);
  };

  const downElement = (el: PidElement, ev: React.PointerEvent) => {
    if (!edit || ev.button !== 0 || edit.bgMode) return; // bg mode: falls through to the stage
    ev.stopPropagation();
    let ids: string[];
    if (ev.shiftKey || ev.ctrlKey || ev.metaKey) {
      // toggle membership
      const had = edit.selected.has(el.id);
      ids = had ? [...edit.selected].filter((i) => i !== el.id) : [...edit.selected, el.id];
      edit.onSelect(ids);
      if (had) return;
    } else if (edit.selected.has(el.id)) {
      ids = [...edit.selected];
    } else {
      ids = [el.id];
      edit.onSelect(ids);
    }
    const origin = new Map<string, [number, number]>();
    for (const e of cfg.elements) if (ids.includes(e.id)) origin.set(e.id, [e.x, e.y]);
    gesture.current = {
      kind: "move",
      primary: el.id,
      start: toNorm(ev),
      origin,
      moved: false,
      dup: ev.altKey,
    };
    capture(ev);
  };

  const downHandle = (el: PidElement, ev: React.PointerEvent) => {
    if (!edit || ev.button !== 0) return;
    ev.stopPropagation();
    const r = outer.current!.getBoundingClientRect();
    const center: [number, number] = [
      r.left + box.left + el.x * box.w,
      r.top + box.top + el.y * box.h,
    ];
    const origin = new Map<string, number>();
    for (const e of cfg.elements) if (edit.selected.has(e.id)) origin.set(e.id, elSize(e, box.w));
    gesture.current = {
      kind: "resize",
      center,
      d0: Math.max(4, Math.hypot(ev.clientX - center[0], ev.clientY - center[1])),
      origin,
      moved: false,
    };
    capture(ev);
  };

  const downStage = (ev: React.PointerEvent) => {
    if (!edit || ev.button !== 0) return;
    if (edit.bgMode) return downBg(ev, false);
    gesture.current = {
      kind: "marquee",
      start: toNorm(ev),
      base: ev.shiftKey || ev.ctrlKey || ev.metaKey ? [...edit.selected] : [],
      moved: false,
    };
    capture(ev);
  };

  const move = (ev: React.PointerEvent) => {
    if (!edit) return;
    const [nx, ny] = toNorm(ev);
    edit.cursor.current =
      nx >= 0 && nx <= 1 && ny >= 0 && ny <= 1 ? [+nx.toFixed(4), +ny.toFixed(4)] : null;
    const g = gesture.current;
    if (!g) return;

    if (g.kind === "marquee") {
      const [sx, sy] = g.start;
      if (!g.moved && Math.hypot((nx - sx) * box.w, (ny - sy) * box.h) < 3) return;
      g.moved = true;
      setMarquee([Math.min(sx, nx), Math.min(sy, ny), Math.max(sx, nx), Math.max(sy, ny)]);
      return;
    }

    if (g.kind === "bg") {
      const dx = nx - g.start[0];
      const dy = ny - g.start[1];
      if (!g.moved) {
        if (Math.hypot(dx * box.w, dy * box.h) < 3) return;
        g.moved = true;
        edit.checkpoint();
      }
      const next = clampBg(
        g.resize
          ? {
              // uniform, anchored at the image's top-left corner
              ...g.bg0,
              scale:
                (g.bg0.scale * Math.hypot((nx - g.bg0.x) * box.w, (ny - g.bg0.y) * box.h)) / g.d0,
            }
          : { ...g.bg0, x: g.bg0.x + dx, y: g.bg0.y + dy },
      );
      const els: Record<string, { x: number; y: number; size?: number }> = {};
      if (edit.bgFollow) for (const e of g.els) els[e.id] = followEl(e, g.bg0, next);
      edit.patchBg?.(next, els);
      return;
    }

    if (g.kind === "resize") {
      const f = Math.hypot(ev.clientX - g.center[0], ev.clientY - g.center[1]) / g.d0;
      if (!g.moved) {
        if (Math.abs(f - 1) < 0.02) return;
        g.moved = true;
        edit.checkpoint();
      }
      const patches: Record<string, { size: number }> = {};
      for (const e of cfg.elements) {
        const s0 = g.origin.get(e.id);
        if (s0 !== undefined) patches[e.id] = { size: clampSize(e.type, s0 * f) };
      }
      edit.patch(patches);
      return;
    }

    let dx = nx - g.start[0];
    let dy = ny - g.start[1];
    if (!g.moved) {
      if (Math.hypot(dx * box.w, dy * box.h) < 3) return;
      g.moved = true;
      if (g.dup) {
        // the copies take over the gesture; the originals stay put
        const ids = [...g.origin.keys()];
        const copies = edit.duplicate(ids);
        const origin = new Map<string, [number, number]>();
        ids.forEach((id, i) => origin.set(copies[i], g.origin.get(id)!));
        g.primary = copies[ids.indexOf(g.primary)];
        g.origin = origin;
      } else {
        edit.checkpoint();
      }
    }
    if (ev.shiftKey) {
      // constrain to the dominant axis
      if (Math.abs(dx * box.w) > Math.abs(dy * box.h)) dy = 0;
      else dx = 0;
    }
    const [ox, oy] = g.origin.get(g.primary)!;
    let tx = ox + dx;
    let ty = oy + dy;
    const guide: { x?: number; y?: number } = {};
    if (!ev.ctrlKey && !ev.metaKey) {
      if (edit.snap) {
        const stepY = (GRID * box.w) / (box.h || 1);
        tx = Math.round(tx / GRID) * GRID;
        ty = Math.round(ty / stepY) * stepY;
      }
      // line up with the nearest other element (wins over the grid)
      let bestX = 5 / (box.w || 1);
      let bestY = 5 / (box.h || 1);
      const px = ox + dx;
      const py = oy + dy;
      for (const o of cfg.elements) {
        if (g.origin.has(o.id)) continue;
        if (Math.abs(o.x - px) < bestX) {
          bestX = Math.abs(o.x - px);
          tx = guide.x = o.x;
        }
        if (Math.abs(o.y - py) < bestY) {
          bestY = Math.abs(o.y - py);
          ty = guide.y = o.y;
        }
      }
    }
    dx = tx - ox;
    dy = ty - oy;
    // keep the whole selection on the canvas
    for (const [x, y] of g.origin.values()) {
      dx = Math.min(1 - x, Math.max(-x, dx));
      dy = Math.min(1 - y, Math.max(-y, dy));
    }
    const patches: Record<string, { x: number; y: number }> = {};
    for (const [id, [x, y]] of g.origin) patches[id] = { x: +(x + dx).toFixed(4), y: +(y + dy).toFixed(4) };
    edit.patch(patches);
    setGuides(guide.x !== undefined || guide.y !== undefined ? guide : null);
  };

  const up = () => {
    const g = gesture.current;
    gesture.current = null;
    setGuides(null);
    if (!edit || g?.kind !== "marquee") return;
    if (!g.moved || !marquee) {
      edit.onSelect(g.base);
    } else {
      const [x0, y0, x1, y1] = marquee;
      const hit = cfg.elements
        .filter((e) => e.x >= x0 && e.x <= x1 && e.y >= y0 && e.y <= y1)
        .map((e) => e.id);
      edit.onSelect([...new Set([...g.base, ...hit])]);
    }
    setMarquee(null);
  };

  const gridPx = GRID * box.w;

  return (
    <div
      ref={outer}
      className={`pid-stage ${edit ? "editing" : ""} ${edit?.bgMode ? "bg-mode" : ""}`}
      onPointerDown={downStage}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onPointerLeave={() => {
        if (edit && !gesture.current) edit.cursor.current = null;
      }}
    >
      {cfg.background && (
        <img
          src={`/api/pid/assets/${cfg.background}`}
          alt=""
          draggable={false}
          onLoad={(e) =>
            setNatural({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
          }
          style={{
            position: "absolute",
            left: box.left + bg.x * box.w,
            top: box.top + bg.y * box.h,
            width: box.w * bg.scale || undefined,
            height: box.h * bg.scale || undefined,
            maxWidth: "none",
            userSelect: "none",
          }}
        />
      )}
      {edit?.bgMode && cfg.background && (
        <div
          className="pid-bg-frame"
          style={{
            left: box.left + bg.x * box.w,
            top: box.top + bg.y * box.h,
            width: box.w * bg.scale,
            height: box.h * bg.scale,
          }}
        >
          <span className="pid-handle" onPointerDown={(ev) => downBg(ev, true)} />
        </div>
      )}
      {edit?.snap && gridPx >= 4 && (
        <div
          className="pid-grid"
          style={{
            left: box.left,
            top: box.top,
            width: box.w,
            height: box.h,
            backgroundSize: `${gridPx}px ${gridPx}px`,
          }}
        />
      )}
      {cfg.elements.map((el) => {
        const cx = box.left + el.x * box.w;
        const cy = box.top + el.y * box.h;
        const selected = edit?.selected.has(el.id) ?? false;
        const rotation = el.type === "sensor" ? 0 : (el.rotation ?? 0);
        const common = {
          onPointerDown: edit ? (ev: React.PointerEvent) => downElement(el, ev) : undefined,
          onClick: edit ? undefined : () => onElementClick?.(el),
        };
        const style: React.CSSProperties = {
          left: cx,
          top: cy,
          transform: rotation ? `translate(-50%, -50%) rotate(${rotation}deg)` : undefined,
        };
        const handle = edit && !edit.bgMode && selected && (
          <span className="pid-handle" onPointerDown={(ev) => downHandle(el, ev)} />
        );
        if (el.type === "valve") {
          const kind = valveKind(el.channel);
          const px = Math.max(elSize(el) * box.w, 14);
          const active = liveValve ? liveValve(el.channel) : null;
          return (
            <div
              key={el.id}
              {...common}
              className={`pid-el pid-valve ${selected ? "selected" : ""}`}
              title={el.channel}
              style={style}
            >
              <ValveSymbol kind={kind} active={active} px={px} rotation={0} selected={selected} />
              {handle}
            </div>
          );
        }
        if (el.type === "sensor") {
          const v = liveValue ? liveValue(el.channel) : null;
          const stale = staleOf ? staleOf(el.channel) : false;
          const warn =
            !stale &&
            v !== null &&
            ((el.warn_min !== undefined && v < el.warn_min) ||
              (el.warn_max !== undefined && v > el.warn_max));
          const scaled = el.size != null;
          return (
            <div
              key={el.id}
              {...common}
              className={`pid-el pid-sensor ${warn ? "warn" : ""} ${stale ? "stale" : ""} ${selected ? "selected" : ""}`}
              title={el.channel}
              style={scaled ? { ...style, fontSize: Math.max(el.size! * box.w, 7) } : style}
            >
              {v === null ? "—" : v.toFixed(el.decimals ?? 1)}
              <span className="pid-unit" style={scaled ? { fontSize: "0.83em" } : undefined}>
                {unitOf?.(el.channel) ?? ""}
              </span>
              {handle}
            </div>
          );
        }
        return (
          <div
            key={el.id}
            {...common}
            className={`pid-el pid-label ${selected ? "selected" : ""}`}
            style={{ ...style, fontSize: Math.max(elSize(el) * box.w, 9) }}
          >
            {el.text}
            {handle}
          </div>
        );
      })}
      {guides?.x !== undefined && (
        <div className="pid-guide v" style={{ left: box.left + guides.x * box.w, top: box.top, height: box.h }} />
      )}
      {guides?.y !== undefined && (
        <div className="pid-guide h" style={{ top: box.top + guides.y * box.h, left: box.left, width: box.w }} />
      )}
      {marquee && (
        <div
          className="pid-marquee"
          style={{
            left: box.left + marquee[0] * box.w,
            top: box.top + marquee[1] * box.h,
            width: (marquee[2] - marquee[0]) * box.w,
            height: (marquee[3] - marquee[1]) * box.h,
          }}
        />
      )}
      {children?.(box)}
    </div>
  );
}
