import { useEffect, useMemo, useState } from "react"
import type { DataSnapshot } from "./types"
import { fetchRecords } from "./api"
import { TeamButton } from "./Sidebar"

type Props = { onClose: () => void; onOpenTeam: () => void; teamOpen: boolean }

const MAX_COLUMNS = 6
const REFRESH_MS = 10_000  // the bots write while the page is open

function cell(value: unknown): string {
  if (value === null || value === undefined) return ""
  return typeof value === "object" ? JSON.stringify(value) : String(value)
}

export function DataView({ onClose, onOpenTeam, teamOpen }: Props) {
  const [snap, setSnap] = useState<DataSnapshot | null>(null)
  const [collection, setCollection] = useState("")
  const [error, setError] = useState("")
  const [open, setOpen] = useState("")

  useEffect(() => {
    let live = true
    const load = () => fetchRecords(collection).then((s) => { if (live) { setSnap(s); setError("") } }, (e) => { if (live) setError(String(e.message || e)) })
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => { live = false; clearInterval(timer) }
  }, [collection])

  // one column per field the records have, the most common first: records of a collection need not share a shape
  const columns = useMemo(() => {
    const seen = new Map<string, number>()
    for (const r of snap?.records ?? []) for (const k of Object.keys(r.data)) seen.set(k, (seen.get(k) ?? 0) + 1)
    return [...seen].sort((a, b) => b[1] - a[1]).slice(0, MAX_COLUMNS).map(([k]) => k)
  }, [snap])

  return (
    <div className="cal">
      <header className="cal-bar">
        <TeamButton onClick={onOpenTeam} open={teamOpen} />
        <div className="cal-heading">
          <h2 className="cal-title">Data</h2>
          <p className="cal-range">{snap?.records.length ?? 0} shown in {snap?.collection || "no collection yet"}</p>
        </div>
        {snap && snap.collections.length > 0 && (
          <div className="cal-segment" role="tablist" aria-label="Collection">
            {snap.collections.map((c) => (
              <button key={c.name} type="button" role="tab" aria-selected={snap.collection === c.name} onClick={() => { setCollection(c.name); setOpen("") }}>
                {c.name} · {c.count}
              </button>
            ))}
          </div>
        )}
        <button className="cal-today" onClick={onClose} type="button">← Chat</button>
      </header>
      <div className="data-body">
        {error && <p className="data-empty">{error}</p>}
        {!error && snap && snap.collections.length === 0 && (
          <p className="data-empty">Nothing is stored yet. Orders from the published site, and anything the bots save, show up here.</p>
        )}
        {snap && snap.records.length > 0 && (
          <table className="data-table">
            <thead>
              <tr><th>Received</th><th>Status</th>{columns.map((c) => <th key={c}>{c}</th>)}</tr>
            </thead>
            <tbody>
              {snap.records.map((r) => (
                <tr key={r.id} onClick={() => setOpen(open === r.id ? "" : r.id)} className={open === r.id ? "is-open" : ""}>
                  <td>{new Date(r.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</td>
                  <td><span className={`data-status${r.status === "test" ? " is-test" : ""}`}>{r.status || "-"}</span></td>
                  {open === r.id
                    ? <td colSpan={Math.max(columns.length, 1)}><pre className="data-json">{JSON.stringify(r.data, null, 2)}</pre></td>
                    : columns.map((c) => <td key={c}>{cell(r.data[c])}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
