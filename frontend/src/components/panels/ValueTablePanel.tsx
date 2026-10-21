import { useMemo } from "react";
import { buffers } from "../../databuf";
import { LiveValue } from "../LiveValue";
import { useStore } from "../../store";
import { isReadable, type PanelConfig } from "../../types";

function globToRegex(glob: string): RegExp {
  const esc = glob.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp(`^${esc}$`);
}

const fmt = (v: number | null) => (v === null ? "—" : v.toFixed(2));

// grey the whole row out when its channel stops updating
const markStale = (tag: string) => (el: HTMLSpanElement) => {
  const row = el.closest("tr");
  const stale = buffers.isStale(tag);
  if (row && row.classList.contains("stale") !== stale) {
    row.classList.toggle("stale", stale);
    row.title = stale ? "no fresh data" : "";
  }
};

export function ValueTablePanel({ panel }: { panel: PanelConfig }) {
  const channels = useStore((s) => s.channels);

  const tags = useMemo(() => {
    const inputs = channels.filter(isReadable);
    const patterns = panel.channels.map(globToRegex);
    return inputs.filter((c) => patterns.some((p) => p.test(c.tag)));
  }, [channels, panel.channels]);

  return (
    <div style={{ overflowY: "auto", height: "100%" }}>
      <table className="value-table">
        <tbody>
          {tags.map((c) => (
            <tr key={c.tag}>
              <td className="tag">
                {c.tag}
                {c.name && <span className="cname">{c.name}</span>}
              </td>
              <td className="val">
                <LiveValue tag={c.tag} format={fmt} onUpdate={markStale(c.tag)} />
              </td>
              <td className="unit">{c.unit}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
