import { useEffect, useState } from "react";

interface RawRow {
  tag: string;
  hw: string;
  kind: string;
  unit: string;
  rate_hz: number;
  raw: number | null;
  value: number | null;
}

export function RawTablePanel() {
  const [rows, setRows] = useState<RawRow[]>([]);
  const [peekName, setPeekName] = useState("");
  const [peek, setPeek] = useState("");

  useEffect(() => {
    const poll = () =>
      fetch("/api/raw")
        .then((r) => r.json())
        .then((d) => setRows(d.channels))
        .catch(() => {});
    poll();
    const iv = setInterval(poll, 500);
    return () => clearInterval(iv);
  }, []);

  const doPeek = () => {
    if (!peekName.trim()) return;
    fetch(`/api/connection/read?name=${encodeURIComponent(peekName.trim())}`)
      .then(async (r) => {
        const d = await r.json();
        setPeek(r.ok ? `${d.name} = ${d.value}` : String(d.detail));
      })
      .catch((e) => setPeek(String(e)));
  };

  return (
    <div style={{ overflowY: "auto", height: "100%", display: "flex", flexDirection: "column" }}>
      <table className="value-table">
        <thead>
          <tr style={{ color: "var(--text-faint)", fontSize: 10, textTransform: "uppercase" }}>
            <td>tag</td>
            <td>pin</td>
            <td style={{ textAlign: "right" }}>raw</td>
            <td style={{ textAlign: "right" }}>value</td>
            <td>unit</td>
            <td style={{ textAlign: "right" }}>rate</td>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr key={c.tag}>
              <td>{c.tag}</td>
              <td style={{ color: "var(--text-dim)" }}>{c.hw}</td>
              <td className="val" style={{ color: "var(--text)" }}>
                {c.raw === null ? "—" : `${c.raw.toFixed(5)} V`}
              </td>
              <td className="val">{c.value === null ? "—" : c.value.toFixed(3)}</td>
              <td className="unit">{c.unit}</td>
              <td style={{ textAlign: "right", color: "var(--text-faint)" }}>{c.rate_hz} Hz</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ display: "flex", gap: 4, padding: "6px 8px", marginTop: "auto" }}>
        <input
          className="modal-input"
          placeholder="peek register e.g. AIN10, DIO_STATE"
          value={peekName}
          onChange={(e) => setPeekName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && doPeek()}
        />
        <button className="btn" onClick={doPeek}>
          read
        </button>
      </div>
      {peek && (
        <div style={{ fontFamily: "var(--mono)", fontSize: 11, padding: "0 8px 6px" }}>{peek}</div>
      )}
    </div>
  );
}
