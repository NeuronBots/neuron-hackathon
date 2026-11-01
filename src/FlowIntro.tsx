import { useEffect, useRef, useState, type CSSProperties } from "react"
import { fetchFlowIntro } from "./api"
import { NeuronLoader } from "./NeuronLoader"
import { SourceCards } from "./SourceCards"
import { TeamCard } from "./TeamCard"
import type { FlowIntroData } from "./types"
const LEAD = { id: "demo-m", name: "Master" }
const MUSE = ["muse exec --workspace /repo", "Reading the plan and the skills", "Writing package.json", "$ npm install", "added 142 packages in 9s", "Writing src/App.tsx", "Writing src/Checkout.tsx", "$ npm run build", "✓ 38 modules transformed", "Checking the app answers ... ok"]

type Stage = "wait" | "burst" | "welcome" | "fade" | "done"
type Step = { id: string; label: string; what: string; you: string }
const GENERIC: Step[] = [
  { id: "plan", label: "Plan", what: "Your team studies the goal, asks each other, and writes one detailed plan for the first build.", you: "Answer the few questions that show up in Needs you." },
  { id: "build", label: "Build", what: "Muse builds the first version in a sandbox, and you can watch it work in a terminal.", you: "Read the plan, then press Approve and build." },
  { id: "live", label: "Live", what: "When the build is done, a publish card appears. Nothing goes public before that.", you: "Press Allow on the publish card to put the site online." },
  { id: "orders", label: "Orders", what: "When a visitor orders, your bots can email, record and follow up. Each one starts on its own.", you: "Tell the Master what each bot should do on an order, and approve it." },
  { id: "payments", label: "Payments", what: "Turn on payments and customers pay on your site. The money lands in your Wallet.", you: "Turn payments on and approve your prices. Refunds wait for your yes." },
]

// A scripted run of one stage, looping while the step is on screen: no bot, no model call
const GENERIC_DEMO: FlowIntroData["demo"] = { question: "Which currency do you sell in?", options: ["INR", "USD"], customer: "Asha Rao", item: "Your first product", price: 500, currency: "INR" }
const LABELS: Record<string, string> = { plan: "Plan", build: "Build", live: "Live", orders: "Orders", payments: "Payments" }

function Demo({ id, tick, demo, names }: { id: string; tick: number; demo: FlowIntroData["demo"]; names: string[] }) {
  const TEAM = [0, 1, 2].map((i) => ({ id: `demo-${i}`, name: names[i] ?? ["Jasmine", "Amber", "Rose"][i] }))
  if (id === "plan") {
    const talking = tick < 2 ? [TEAM[0]] : tick < 4 ? [TEAM[1]] : []
    const done = tick < 2 ? [] : tick < 4 ? [TEAM[0]] : [TEAM[0], TEAM[1]]
    return (
      <>
        <TeamCard lead={LEAD} done={done} talking={talking} />
        {tick >= 5 && (
          <div className="dm-share src-approval flow-demo-ask" aria-hidden="true">
            <p className="dm-share-q src-run-head is-waiting">{demo.question}</p>
            <div className="src-decision-options">{demo.options.map((o, i) => <button key={o} type="button" className={i === 0 ? "dm-share-yes" : "dm-share-no"} tabIndex={-1}>{o}</button>)}</div>
          </div>
        )}
      </>
    )
  }
  if (id === "build") {
    return (
      <div className="src-terminal" aria-hidden="true">
        <div className="src-terminal-bar"><span className="src-terminal-dots"><i /><i /><i /></span><span>muse</span><span className="src-terminal-n">{Math.min(tick + 1, MUSE.length)} lines</span></div>
        <pre className="src-terminal-screen">{MUSE.slice(0, tick + 1).map((l, i) => <div key={i}>{l}</div>)}</pre>
      </div>
    )
  }
  if (id === "live") {
    return (
      <div className="dm-share src-approval flow-demo-ask" aria-hidden="true">
        <p className="dm-share-q src-run-head is-waiting">Muse wants to publish your site. Allow it?</p>
        <div className="dm-share-actions"><button type="button" className="dm-share-no" tabIndex={-1}>Decline</button><button type="button" className={`dm-share-yes${tick >= 3 ? " flow-pressed" : ""}`} tabIndex={-1}>{tick >= 3 ? "Allowed" : "Allow"}</button></div>
        {tick >= 4 && <span className="dm-share-done">Live at your-shop.neuron.site</span>}
      </div>
    )
  }
  if (id === "orders") {
    return tick >= 1 ? <SourceCards card={{ kind: "order", who: demo.customer, items: [`2× ${demo.item}`], total: demo.price * 2, currency: demo.currency, details: [], ref: "abcdef12", next: "2 order routines started" }} /> : null
  }
  return tick >= 1 ? <SourceCards card={{ kind: "payment", amount: Math.round(demo.price * 200), currency: demo.currency, net: Math.round(demo.price * 190), fee: Math.round(demo.price * 10), who: demo.customer, items: [`2× ${demo.item}`], ref: "abcdef12", payout: "in 7 days" }} /> : null
}

// After a project is made: what the next stages are, what each one shows, and what you do. Plays a short demo of each.
export function FlowIntro({ name, onDone: finish, data: given, hold = false }: { name: string; onDone: () => void; data?: FlowIntroData; hold?: boolean }) {
  const [at, setAt] = useState(0)
  const [closing, setClosing] = useState(false)
  const onDone = () => { if (closing) return; setClosing(true); window.setTimeout(finish, 220) }  // the modal eases out before it goes
  const [dir, setDir] = useState<"next" | "prev">("next")
  const [leaving, setLeaving] = useState<{ step: Step; dir: "next" | "prev"; n: number } | null>(null)  // the stage that is sliding out while the next slides in
  const go = (i: number) => {
    if (i === at || i < 0 || i >= STEPS.length) return
    const way = i > at ? "next" : "prev"
    setDir(way)
    setLeaving({ step: STEPS[at], dir: way, n: Date.now() })
    setAt(i)
  }
  const goRef = useRef(go)
  goRef.current = go
  const [data, setData] = useState<FlowIntroData | null>(given ?? null)
  const [failed, setFailed] = useState(false)
  useEffect(() => { if (given) setData(given) }, [given])
  useEffect(() => { if (!given && !hold) fetchFlowIntro().then(setData, () => setFailed(true)) }, [given, hold])  // written for this project; the plain version if it cannot be
  const STEPS: Step[] = data ? data.steps.map((s) => ({ ...s, label: LABELS[s.id] ?? s.id })) : GENERIC
  const [tick, setTick] = useState(0)
  const step = STEPS[at]
  const atRef = useRef(at)
  atRef.current = at
  useEffect(() => { if (!leaving) return; const t = window.setTimeout(() => setLeaving(null), 520); return () => window.clearTimeout(t) }, [leaving])
  useEffect(() => {
    setTick(0)
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { setTick(9); return }
    const timer = window.setInterval(() => setTick((t) => (t >= 9 ? 0 : t + 1)), 1000)
    return () => window.clearInterval(timer)
  }, [at])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onDone()
      if (e.key === "ArrowRight") goRef.current(atRef.current + 1)
      if (e.key === "ArrowLeft") goRef.current(atRef.current - 1)
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onDone])
  const last = at === STEPS.length - 1
  // When the text is ready the lines of the N stretch lengthwise until the modal is all white, it says welcome, and the white lifts to show the content
  const loaded = Boolean(data) || failed
  const [stage, setStage] = useState<Stage>(loaded ? "done" : "wait")
  useEffect(() => {
    if (stage === "wait") {
      if (loaded) setStage(window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "done" : "burst")
      return
    }
    const next: Partial<Record<Stage, [Stage, number]>> = { burst: ["welcome", 1300], welcome: ["fade", 1500], fade: ["done", 650] }
    const step = next[stage]
    if (!step) return
    const timer = window.setTimeout(() => setStage(step[0]), step[1])
    return () => window.clearTimeout(timer)
  }, [loaded, stage])
  const shown = stage === "fade" || stage === "done"
  return (
    <div className={`profile-overlay flow-overlay${closing ? " is-closing" : ""}`} onClick={onDone}>
      <div className={`profile-modal flow-intro${closing ? " is-closing" : ""}`} role="dialog" aria-modal="true" aria-label={`How ${name} gets built`} onClick={(e) => e.stopPropagation()}>
        {shown && <h2 className={`pay-offer-head src-run-head is-running flow-head${stage === "fade" ? " flow-reveal" : ""}`}><span className="pay-offer-kicker">{name} is ready</span><span>Here is how it gets built</span></h2>}
        {!shown ? (
          <div className="flow-waiting">{stage === "wait" && <NeuronLoader size={140} label={`Writing the steps for ${name}…`} />}</div>
        ) : (
          <div className={stage === "fade" ? "flow-reveal" : undefined}>
          <ol className="flow-steps" aria-label="Stages" style={{ "--at": at } as CSSProperties}>
            <span className="flow-indicator" aria-hidden="true" />
            {STEPS.map((s, i) => (
              <li key={s.id} className={i === at ? "is-current" : i < at ? "is-done" : ""}><button type="button" onClick={() => go(i)} aria-current={i === at ? "step" : undefined}>{s.label}</button></li>
            ))}
          </ol>
          <div className="flow-body">
            {leaving && (
              <div key={`out-${leaving.n}`} className={`flow-slide-out flow-slide-out-${leaving.dir}`} aria-hidden="true">
                <p className="flow-what">{leaving.step.what}</p>
                <div className="flow-demo"><Demo id={leaving.step.id} tick={tick} demo={data?.demo ?? GENERIC_DEMO} names={data?.team ?? []} /></div>
                <p className="flow-you"><span>You</span>{leaving.step.you}</p>
              </div>
            )}
            <div key={step.id} className={`flow-slide flow-slide-${dir}`}>
            <p className="flow-what">{step.what}</p>
            <div className="flow-demo" key={step.id}><Demo id={step.id} tick={tick} demo={data?.demo ?? GENERIC_DEMO} names={data?.team ?? []} /></div>
            <p className="flow-you"><span>You</span>{step.you}</p>
            </div>
          </div>
          <div className="pay-faq-nav">
            <button type="button" className="dm-share-no" disabled={at === 0} onClick={() => go(at - 1)}>← Back</button>
            <span className="pay-faq-dots" aria-hidden="true">{STEPS.map((s, i) => <i key={s.id} className={i === at ? "is-on" : ""} />)}</span>
            {last ? <button type="button" className="dm-share-yes" onClick={onDone} autoFocus>Start</button> : <button type="button" className="dm-share-no" onClick={() => go(at + 1)}>Next →</button>}
          </div>
          </div>
        )}
        {(stage === "burst" || stage === "welcome" || stage === "fade") && (
          <div className={`flow-burst is-${stage}`} aria-hidden={stage === "burst" ? true : undefined}>
            {stage === "burst" && <NeuronLoader size={140} />}
            {stage !== "burst" && <p className="flow-welcome"><span>{name} is ready</span>Welcome to Neuron</p>}
          </div>
        )}
        {(!shown || !last) && <button type="button" className="flow-skip" onClick={onDone}>Skip, take me to the chat</button>}
      </div>
    </div>
  )
}
