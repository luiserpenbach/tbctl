import { useEffect, useState } from "react";
import {
  activateChecklist,
  checkChecklistItem,
  fetchChecklists,
  type ChecklistState,
} from "../../api";

// Procedural checklist (roadmap phase 15): checking items logs events and
// the progress is shown next to ARM in the sequence panel. Nothing blocks —
// discipline, not interlock. Active state lives on the node so every
// connected client sees the same list.

export function ChecklistPanel() {
  const [lists, setLists] = useState<{ name: string; items: string[] }[]>([]);
  const [active, setActive] = useState<ChecklistState | null>(null);
  const [err, setErr] = useState("");

  const refresh = () =>
    fetchChecklists()
      .then((r) => {
        setLists(r.checklists);
        setActive(r.active);
      })
      .catch((e) => setErr(String(e)));
  useEffect(() => {
    refresh();
    const iv = setInterval(refresh, 3000); // pick up checks made elsewhere
    return () => clearInterval(iv);
  }, []);

  const done = active ? active.checked.filter(Boolean).length : 0;

  return (
    <div style={{ padding: 8, display: "flex", flexDirection: "column", gap: 6, height: "100%" }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        <select
          className="seq-select"
          value={active?.name ?? ""}
          onChange={(e) =>
            activateChecklist(e.target.value || null)
              .then((r) => setActive(r.active))
              .catch((x) => setErr(String(x)))
          }
        >
          <option value="">— no checklist —</option>
          {lists.map((l) => (
            <option key={l.name} value={l.name}>
              {l.name}
            </option>
          ))}
        </select>
        {active && (
          <span className={`chip ${done === active.items.length ? "ok" : "warn"}`}>
            <span className="dot" />
            {done}/{active.items.length}
          </span>
        )}
      </div>
      {active && (
        <div style={{ overflowY: "auto", flex: 1 }}>
          {active.items.map((item, i) => (
            <label key={i} className={`checklist-item ${active.checked[i] ? "done" : ""}`}>
              <input
                type="checkbox"
                checked={active.checked[i]}
                onChange={(e) =>
                  checkChecklistItem(i, e.target.checked)
                    .then((r) => setActive(r.active))
                    .catch((x) => setErr(String(x)))
                }
              />
              <span>{item}</span>
            </label>
          ))}
        </div>
      )}
      {!active && <div className="placeholder">select a checklist</div>}
      {err && <div style={{ color: "var(--red)", fontSize: 11 }}>{err}</div>}
    </div>
  );
}
