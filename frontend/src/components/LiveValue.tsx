import { useEffect, useRef } from "react";
import { buffers, onSlowTick } from "../databuf";

/**
 * A live reading that rewrites its own text at 10 Hz without re-rendering
 * the list around it. `onUpdate` may restyle the element (stale, limits).
 */
export function LiveValue({
  tag,
  format,
  onUpdate,
}: {
  tag: string;
  format: (v: number | null) => string;
  onUpdate?: (el: HTMLSpanElement, v: number | null) => void;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const formatRef = useRef(format);
  const onUpdateRef = useRef(onUpdate);
  formatRef.current = format;
  onUpdateRef.current = onUpdate;

  useEffect(() => {
    const update = () => {
      const el = ref.current;
      if (!el) return;
      const v = buffers.latest(tag);
      const text = formatRef.current(v);
      if (el.textContent !== text) el.textContent = text;
      onUpdateRef.current?.(el, v);
    };
    update();
    return onSlowTick(update);
  }, [tag]);

  return <span ref={ref} />;
}
