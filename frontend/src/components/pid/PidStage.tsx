import { useEffect, useRef, useState } from "react";
import type { PidElement, PidConfigT } from "../../api";

// Shared P&ID rendering surface (roadmap phase 13): background SVG in an
// aspect-preserving content box, elements absolutely positioned at
// normalized 0-1 coordinates against that box. The viewer panel passes live
// state accessors; the editor passes drag/select callbacks instead.

export interface StageBox {
  left: number;
  top: number;
  w: number;
  h: number;
}

export function ValveSymbol({
  kind,
  active,
  px,
  rotation,
  selected,
}: {
  kind: "valve" | "spark";
  active: boolean | null; // null = unknown
  px: number;
  rotation: number;
  selected?: boolean;
}) {
  const stroke = selected ? "var(--accent)" : active === null ? "var(--text-faint)" : "#aab4c4";
  if (kind === "spark") {
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

export function PidStage({
  cfg,
  valveKind,
  liveValve,
  liveValue,
  staleOf,
  unitOf,
  onElementClick,
  onElementDrag,
  selectedId,
  editing,
  children,
}: {
  cfg: PidConfigT;
  valveKind: (channel: string) => "valve" | "spark";
  liveValve?: (channel: string) => boolean | null;
  liveValue?: (channel: string) => number | null;
  staleOf?: (channel: string) => boolean;
  unitOf?: (channel: string) => string;
  onElementClick?: (el: PidElement) => void;
  onElementDrag?: (id: string, x: number, y: number) => void;
  selectedId?: string | null;
  editing?: boolean;
  children?: (box: StageBox) => React.ReactNode; // overlay (confirm popover)
}) {
  const outer = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [natural, setNatural] = useState<{ w: number; h: number } | null>(null);
  const drag = useRef<{ id: string } | null>(null);

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

  const toNorm = (ev: { clientX: number; clientY: number }): [number, number] => {
    const r = outer.current!.getBoundingClientRect();
    const x = (ev.clientX - r.left - box.left) / (box.w || 1);
    const y = (ev.clientY - r.top - box.top) / (box.h || 1);
    return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))];
  };

  return (
    <div
      ref={outer}
      className={`pid-stage ${editing ? "editing" : ""}`}
      onPointerMove={(ev) => {
        if (drag.current && onElementDrag) {
          const [x, y] = toNorm(ev);
          onElementDrag(drag.current.id, +x.toFixed(4), +y.toFixed(4));
        }
      }}
      onPointerUp={() => (drag.current = null)}
      onPointerLeave={() => (drag.current = null)}
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
            left: box.left,
            top: box.top,
            width: box.w || undefined,
            height: box.h || undefined,
            userSelect: "none",
          }}
        />
      )}
      {cfg.elements.map((el) => {
        const cx = box.left + el.x * box.w;
        const cy = box.top + el.y * box.h;
        const common = {
          key: el.id,
          onPointerDown: (ev: React.PointerEvent) => {
            if (editing && onElementDrag) {
              drag.current = { id: el.id };
              (ev.target as Element).releasePointerCapture?.(ev.pointerId);
            }
          },
          onClick: () => onElementClick?.(el),
        };
        if (el.type === "valve") {
          const kind = valveKind(el.channel);
          const px = Math.max((el.size ?? 0.05) * box.w, 14);
          const active = liveValve ? liveValve(el.channel) : null;
          return (
            <div
              {...common}
              className={`pid-el pid-valve ${selectedId === el.id ? "selected" : ""}`}
              title={el.channel}
              style={{ left: cx, top: cy }}
            >
              <ValveSymbol
                kind={kind}
                active={active}
                px={px}
                rotation={el.rotation ?? 0}
                selected={selectedId === el.id}
              />
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
          return (
            <div
              {...common}
              className={`pid-el pid-sensor ${warn ? "warn" : ""} ${stale ? "stale" : ""} ${selectedId === el.id ? "selected" : ""}`}
              title={el.channel}
              style={{ left: cx, top: cy }}
            >
              {v === null || !Number.isFinite(v) ? "—" : v.toFixed(el.decimals ?? 1)}
              <span className="pid-unit">{unitOf?.(el.channel) ?? ""}</span>
            </div>
          );
        }
        return (
          <div
            {...common}
            className={`pid-el pid-label ${selectedId === el.id ? "selected" : ""}`}
            style={{ left: cx, top: cy, fontSize: Math.max((el.size ?? 0.022) * box.w, 9) }}
          >
            {el.text}
          </div>
        );
      })}
      {children?.(box)}
    </div>
  );
}
