import { useEffect, useState } from "react"
import { fetchPath } from "./api"
import { COST_NOTE, costText } from "./pathCost"
import type { FlowPath } from "./types"

const REFRESH_MS = 30_000

// The project's stages, where it is, and what each cost: the owner's map of the flows
export function PathRail({ projectId, refresh, onJump }: { projectId: string; refresh: string; onJump: (phase: string) => void }) {
  const [path, setPath] = useState<FlowPath | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let live = true
    setPath(null)
    if (!projectId) return
    const load = () => fetchPath().then((p) => { if (live) setPath(p) }, () => { if (live) setPath(null) })  // not the owner: no rail
    void load()
    const timer = setInterval(load, REFRESH_MS)
    return () => { live = false; clearInterval(timer) }
  }, [projectId, refresh])

  if (!path) return null
  const allDone = path.steps.every((s) => s.state === "done")
  return (
    <nav className="path-rail" aria-label="Project path">
      {allDone ? (
        <button type="button" className="path-title path-fold" aria-expanded={open} onClick={() => setOpen((o) => !o)}>Path · {path.steps.length}/{path.steps.length} ✓ <span aria-hidden="true">{open ? "▾" : "▸"}</span></button>
      ) : (
        <h3 className="path-title">Path</h3>
      )}
      {(!allDone || open) && <ol>
        {path.steps.map((step) => (
          <li key={step.id} className={`path-step is-${step.state}`} title={step.state === "pending" ? step.hint : step.cost ? COST_NOTE : step.hint}>
            <button type="button" onClick={() => onJump(step.id)} disabled={!step.cost && step.state !== "done"}>
              <span className="path-mark" aria-hidden="true">{step.state === "done" ? "✓" : ""}</span>
              <span className="path-name">{step.label}</span>
              <span className="path-cost">{step.cost ? costText(step.cost) : step.state === "current" ? "next" : ""}</span>
            </button>
          </li>
        ))}
      </ol>}
    </nav>
  )
}
