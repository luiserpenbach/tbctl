import { useEffect, useMemo, useState } from "react";
import { buffers, onTick } from "../databuf";
import { useStore } from "../store";

export function Sidebar() {
  const channels = useStore((s) => s.channels);
  const status = useStore((s) => s.status);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [, setTick] = useState(0);

  useEffect(() => onTick(() => setTick((t) => t + 1)), []);

  const groups = useMemo(() => {
    const q = query.toLowerCase();
    const filtered = channels.filter((c) => c.tag.toLowerCase().includes(q));
    const by = new Map<string, typeof filtered>();
    for (const c of filtered) {
      const ns = c.tag.split(".")[0];
      if (!by.has(ns)) by.set(ns, []);
      by.get(ns)!.push(c);
    }
    return [...by.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [channels, query]);

  const toggle = (ns: string) =>
    setCollapsed((s) => {
      const next = new Set(s);
      if (next.has(ns)) next.delete(ns);
      else next.add(ns);
      return next;
    });

  return (
    <aside className="sidebar">
      <input
        className="search"
        placeholder="filter channels…"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <div className="tree">
        {groups.map(([ns, chans]) => (
          <div key={ns}>
            <div className="group-header" onClick={() => toggle(ns)}>
              {collapsed.has(ns) ? "▸" : "▾"} {ns} ({chans.length})
            </div>
            {!collapsed.has(ns) &&
              chans.map((c) => {
                const isOutput = c.kind === "valve" || c.kind === "spark";
                const val = isOutput
                  ? (status?.outputs[c.tag] ?? "—")
                  : fmt(buffers.latest(c.tag));
                return (
                  <div key={c.tag} className={`chan ${isOutput ? "output" : ""}`} title={c.hw}>
                    <span>{c.tag.slice(ns.length + 1)}</span>
                    <span className="val">
                      {val} {c.unit ?? ""}
                    </span>
                  </div>
                );
              })}
          </div>
        ))}
      </div>
    </aside>
  );
}

function fmt(v: number | null): string {
  if (v === null) return "—";
  const a = Math.abs(v);
  return v.toFixed(a >= 100 ? 0 : a >= 10 ? 1 : 2);
}
