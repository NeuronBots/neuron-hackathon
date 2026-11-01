import { useEffect, useState } from "react"
import type { FlowUsage, PlanPreview } from "./types"
import { flowUsage, previewPlan, runPlan } from "./api"
import { TeamButton } from "./Sidebar"

type Props = { onClose: () => void; onOpenTeam: () => void; teamOpen: boolean }

const MODES = [
  { id: "full", label: "Full", hint: "Every teammate researches on the web. The most tokens." },
  { id: "light", label: "Light", hint: "Short answers from what the team already knows. No web." },
  { id: "reuse", label: "Reuse saved", hint: "Reads the research already saved in Docs. Nobody researches again." },
  { id: "off", label: "Off", hint: "The Master plans alone from the goal." },
]

const n = (value: number) => value.toLocaleString()

export function FlowsView({ onClose, onOpenTeam, teamOpen }: Props) {
  const [research, setResearch] = useState("light")
  const [decisions, setDecisions] = useState(2)
  const [preview, setPreview] = useState<PlanPreview | null>(null)
  const [since, setSince] = useState("")
  const [usage, setUsage] = useState<FlowUsage | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")

  const act = async (work: () => Promise<void>) => {
    setBusy(true)
    setError("")
    try { await work() } catch (e) { setError(e instanceof Error ? e.message : "Something went wrong.") } finally { setBusy(false) }
  }

  // the meter: what the project's bots have spent since the run started
  useEffect(() => {
    if (!since) return
    let live = true
    const load = () => flowUsage(since).then((u) => { if (live) setUsage(u) }, () => {})
    void load()
    const timer = setInterval(load, 5000)
    return () => { live = false; clearInterval(timer) }
  }, [since])

  const mode = MODES.find((m) => m.id === research)

  return (
    <div className="cal">
      <header className="cal-bar">
        <TeamButton onClick={onOpenTeam} open={teamOpen} />
        <div className="cal-heading">
          <h2 className="cal-title">Flows</h2>
          <p className="cal-range">Run one flow on its own, and watch what it costs</p>
        </div>
        <button className="cal-today" onClick={onClose} type="button">&larr; Chat</button>
      </header>
      <div className="data-body">
        <h3 className="wallet-h">Planning</h3>
        <p className="wallet-line">The Master researches with the team, asks you the decisions that matter, and writes the first-build plan. Its instructions are in <code>backend/flows/plan*.md</code>: edit one and run again, no restart.</p>

        <div className="flow-row">
          <span className="flow-label">Research</span>
          <div className="cal-segment" role="tablist" aria-label="Research">
            {MODES.map((m) => (
              <button key={m.id} type="button" role="tab" aria-selected={research === m.id} onClick={() => { setResearch(m.id); setPreview(null); setConfirming(false) }}>{m.label}</button>
            ))}
          </div>
          <span className="flow-hint">{mode?.hint}</span>
        </div>

        <div className="flow-row">
          <span className="flow-label">Questions to you</span>
          <div className="cal-segment" role="tablist" aria-label="Questions">
            {[0, 1, 2, 3, 4, 5].map((c) => (
              <button key={c} type="button" role="tab" aria-selected={decisions === c} onClick={() => { setDecisions(c); setPreview(null); setConfirming(false) }}>{c}</button>
            ))}
          </div>
          <span className="flow-hint">{decisions === 0 ? "None: the Master assumes and lists what it assumed." : `At most ${decisions}.`}</span>
        </div>

        <div className="wallet-refund">
          <button type="button" className="btn" disabled={busy} onClick={() => act(async () => setPreview(await previewPlan(research, decisions)))}>Preview the brief (0 tokens)</button>
          {confirming
            ? <button type="button" className="btn btn-primary" disabled={busy} onClick={() => act(async () => { const r = await runPlan(research, decisions); setSince(r.since); setUsage(null); setConfirming(false) })}>Confirm: run planning, this spends tokens</button>
            : <button type="button" className="btn btn-primary" disabled={busy} onClick={() => setConfirming(true)}>Run planning on this project</button>}
          {confirming && <button type="button" className="btn" onClick={() => setConfirming(false)}>Cancel</button>}
        </div>
        {error && <p className="data-empty">{error}</p>}

        {since && (
          <div className="wallet-card-set flow-meter">
            <div className="wallet-card"><span>Tokens in</span><strong>{n(usage?.input ?? 0)}</strong></div>
            <div className="wallet-card"><span>Tokens out</span><strong>{n(usage?.output ?? 0)}</strong></div>
            <div className="wallet-card"><span>Model calls</span><strong>{n(usage?.calls ?? 0)}</strong></div>
            <div className="wallet-card"><span>Since</span><strong>{new Date(since).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</strong></div>
          </div>
        )}
        {usage && usage.bots.length > 0 && (
          <table className="data-table flow-bots">
            <thead><tr><th>Bot</th><th>In</th><th>Out</th><th>Calls</th></tr></thead>
            <tbody>{usage.bots.map((b) => <tr key={b.name}><td>{b.name}</td><td>{n(b.input)}</td><td>{n(b.output)}</td><td>{b.calls}</td></tr>)}</tbody>
          </table>
        )}

        {preview && (
          <>
            <p className="wallet-line">The brief is {n(preview.words)} words, about {n(preview.tokens)} tokens. Research: {preview.research}{preview.research === "reuse" ? ` (${preview.reusable_docs} saved)` : ""}. Questions: {preview.decisions}.</p>
            <pre className="data-json flow-brief">{preview.text}</pre>
          </>
        )}
      </div>
    </div>
  )
}
