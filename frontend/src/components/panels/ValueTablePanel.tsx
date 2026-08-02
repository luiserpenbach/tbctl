import { useEffect, useMemo, useState } from "react";
import { buffers, onTick } from "../../databuf";
import { useStore } from "../../store";
import { isReadable, type PanelConfig } from "../../types";

function globToRegex(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${esc}$`);
}

export function ValueTablePanel({ panel }: { panel: PanelConfig }) {
  const channels = useStore((s) => s.channels);
  const [, setTick] = useState(0);
  useEffect(() => onTick(() => setTick((t) => t + 1)), []);

  const tags = useMemo(() => {
    const inputs = channels.filter(isReadable);
    const patterns = panel.channels.map(globToRegex);
    return inputs.filter((c) => patterns.some((p) => p.test(c.tag)));
  }, [channels, panel.channels]);

  return (
    <div style={{ overflowY: "auto", height: "100%" }}>
      <table className="value-table">
        <tbody>
          {tags.map((c) => {
            const v = buffers.latest(c.tag);
            const stale = buffers.isStale(c.tag);
            return (
              <tr key={c.tag} className={stale ? "stale" : ""} title={stale ? "no fresh data" : ""}>
                <td>{c.tag}</td>
                <td className="val">{v === null ? "—" : v.toFixed(2)}</td>
                <td className="unit">{c.unit}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
