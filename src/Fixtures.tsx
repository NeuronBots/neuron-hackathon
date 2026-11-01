import { useEffect, useMemo, useState } from "react"
import { ChatView } from "./ChatView"
import { PaymentsOffer } from "./PaymentsOffer"
import { FlowIntro } from "./FlowIntro"
import { ProjectSetup } from "./ProjectSetup"
import type { AgentRecord, ChatMessage } from "./types"

// Open /?fixtures: the real chat drawn from canned messages, one scenario at a time. No bot, no model call, no login.
const card = (kind: string, fields: object) => `[[SOURCES:{"kind": "${kind}", ${JSON.stringify(fields).slice(1)}]]`  // the spacing the backend writes
const bot = (id: string, name: string, is_master = false): AgentRecord => ({ id, name, system_prompt: "", parent_id: null, is_master, field: "", label: is_master ? "Runs the crew" : "Shop Design", created_at: "" })
const MASTER = bot("master", "Master", true)
const TEAM = [bot("jasmine", "Jasmine"), bot("amber", "Amber"), bot("rose", "Rose")]
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()

let n = 0
const say = (content: string, at: number, extra: Partial<ChatMessage> = {}): ChatMessage => ({ id: `f${++n}`, role: "assistant", content, created_at: ago(at), db_id: n, ...extra })
const me = (content: string, at: number) => say(content, at, { role: "user" })
const peer = (from: string, to: string, content: string, at: number) => say(content, at, { from, to, is_peer: true, speaker: from })

type Scenario = { offer?: boolean; name: string; messages: () => ChatMessage[]; asking?: string[]; streaming?: boolean; typing?: boolean }

const SCENARIOS: Scenario[] = [
  { name: "Planning: team finished", messages: () => [
    say(card("phase", { phase: "plan", label: "Planning", note: "The team researches, asks you what matters, and writes the plan." }), 40),
    me("Build me a shop for handmade candles", 39),
    say("On it. Asking the team.", 38),
    peer("Master", "Jasmine", "What look suits candles?", 37), peer("Jasmine", "Master", "Warm, quiet, lots of photos.", 36),
    peer("Master", "Amber", "How should checkout work?", 35), peer("Amber", "Master", "One page, pay online.", 34),
    say(card("step", { text: "Writing the plan…" }), 30), say(card("step", { text: "Saved it to Documents." }), 29),
    say("First build plan: Wick & Wax is ready.", 28),
    say(card("buildplan", { filename: "plan.md", title: "First build plan: Wick & Wax", project_id: "fixture", status: "ready" }), 28),
  ] },
  { name: "Team card: live (asking two)", asking: ["amber", "rose"], streaming: true, messages: () => [
    say(card("phase", { phase: "plan", label: "Planning" }), 5),
    me("Check the plan with the team", 4),
    peer("Master", "Jasmine", "Any look changes?", 3), peer("Jasmine", "Master", "No.", 2),
  ] },
  { name: "Needs you: question and approval", messages: () => [
    say(card("phase", { phase: "plan", label: "Planning" }), 10),
    say(card("decision", { task_id: "t1", name: "Master", title: "Currency", question: "Which currency do you sell in?", options: ["INR", "USD"] }), 9),
    say(card("approval", { task_id: "t2", name: "Daya", title: "Send email", detail: "Create draft to ['you@example.com']" }), 8),
  ] },
  { name: "Build: running and done", messages: () => [
    say(card("phase", { phase: "build", label: "Building", note: "Muse builds it in a sandbox." }), 30),
    say(card("build", { project: "Wick & Wax", status: "running", stage: "Writing the pages", row: 9001 }), 20),
    say(card("phase", { phase: "live", label: "Live" }), 5),
    say(card("build", { project: "Wick & Wax", status: "done", files: 14 }), 4),
  ] },
  { name: "Order and payment received", messages: () => [
    say(card("phase", { phase: "orders", label: "Order routines" }), 20),
    say(card("order", { who: "Asha Rao", items: ["2× Lavender candle", "1× Wick trimmer"], total: 500, currency: "INR", details: ["phone: 98765 43210", "note: Gift wrap please"], ref: "abcdef12", next: "2 order routines started" }), 6),
    say(card("order", { details: ["message: Do you do custom scents?"], ref: "", next: "The Master is looking at it", test: true }), 5),
    say(card("payment", { amount: 50000, currency: "INR", net: 47500, fee: 2500, who: "Asha Rao", items: ["2× Lavender candle", "1× Wick trimmer"], ref: "abcdef12", payout: "08 Nov" }), 5),
    say(card("payment", { amount: 24900, currency: "INR", net: 23700, fee: 1200, test: true, items: ["1× Lavender candle"], ref: "test0001", payout: "08 Nov" }), 2),
  ] },
  { name: "Introducing Payments (modal)", offer: true, messages: () => [say("The first version is built.", 3)] },
  { name: "Build: failed", messages: () => [say(card("build", { project: "Wick & Wax", status: "failed", reason: "The sandbox ran out of time." }), 3)] },
  { name: "Thinking", typing: true, streaming: true, messages: () => [me("What's left to do?", 1)] },
]

// The build terminal asks the server for Muse's output; here a script stands in for it, one more line each time it is asked
const MUSE = ["muse exec --workspace /repo", "Reading the plan.md and the skills", "Writing package.json", "$ npm install", "added 142 packages in 9s", "Writing src/App.tsx", "Writing src/Shop.tsx", "Writing src/Checkout.tsx", "$ npm run build", "vite v6 building for production...", "✓ 38 modules transformed.", "Checking the app answers on :5173 ... ok"]
function fakeBuildLog() {
  const real = window.fetch
  let shown = 0
  window.fetch = (input, init) => {
    const url = typeof input === "string" ? input : input instanceof Request ? input.url : String(input)
    const m = url.match(/\/builds\/9001\/log\?after=(\d+)/)
    if (/\/wallet$/.test(url)) return Promise.resolve(new Response(JSON.stringify({ enabled: false, offer: true, platform_fee_pct: 3, gateway_fee_pct: 2, refund_days: 7, balance: {}, prices: [], proposed_prices: [], payments: [], payouts: [] }), { headers: { "content-type": "application/json" } }))
    const task = url.match(/\/tasks\/(t\d)$/)  // the two waiting cards: a task that is still asking
    if (task) return Promise.resolve(new Response(JSON.stringify({ id: task[1], agent_id: "master", title: "Send email", status: "decision", decisions: [{ at: "", who: "Daya", text: "Approve: Create draft to ['you@example.com']" }], created_at: null, updated_at: null }), { headers: { "content-type": "application/json" } }))
    if (!m) return real(input, init)
    const lines = MUSE.slice(Number(m[1]), ++shown * 2)
    return Promise.resolve(new Response(JSON.stringify({ lines, next: Number(m[1]) + lines.length, running: true }), { headers: { "content-type": "application/json" } }))
  }
  return () => { window.fetch = real }
}

// The walkthrough as the model would write it for a candle shop
const CANDLES = { team: ["Jasmine", "Amber", "Rose"], demo: { question: "Scented or unscented candles first?", options: ["Scented", "Unscented"], customer: "Asha Rao", item: "Lavender soy candle", price: 499, currency: "INR" }, steps: [
  { id: "plan", what: "Jasmine, Amber and Rose study your candle shop, then ask you the few things only you know, like scents and pricing.", you: "Answer the scent and pricing questions in Needs you." },
  { id: "build", what: "Muse builds Wick and Wax in a sandbox: the shop pages, the product grid and checkout.", you: "Read the plan, then press Approve and build." },
  { id: "live", what: "When it is built, a publish card appears. Your candle shop stays private until you allow it.", you: "Press Allow to put Wick and Wax online." },
  { id: "orders", what: "When someone orders a candle, Amber confirms it by email and Rose records it.", you: "Tell the Master what each bot should do on an order, and approve it." },
  { id: "payments", what: "Customers pay for candles on your site, and the money lands in your Wallet after the refund period.", you: "Turn payments on and approve your candle prices." },
] }

// /?fixtures=loading: the loader first, then the text arrives after four seconds and the N bursts open
function LaterIntro() {
  const [data, setData] = useState<typeof CANDLES | undefined>(undefined)
  useEffect(() => { const t = window.setTimeout(() => setData(CANDLES), 4000); return () => window.clearTimeout(t) }, [])
  return <FlowIntro name="Wick and Wax" onDone={() => history.back()} data={data} hold />
}

// /?fixtures=setup: the create-project screens, on the real API (it reads your GitHub and Atlassian links; do not press the last button, that makes a project)
export function Fixtures() {
  if (new URLSearchParams(location.search).get("fixtures") === "loading") return <LaterIntro />
  if (new URLSearchParams(location.search).get("fixtures") === "intro") return <FlowIntro name="Wick and Wax" onDone={() => history.back()} data={CANDLES} />
  if (new URLSearchParams(location.search).get("fixtures") === "setup") return <ProjectSetup onCancel={() => history.back()} onCreated={async () => {}} />
  return <FixturesChat />
}

function FixturesChat() {
  useEffect(fakeBuildLog, [])
  const [at, setAt] = useState(0)
  useEffect(() => { localStorage.removeItem("neuron.paymentsOffer.fixture-offer") }, [at])  // so the modal can be dismissed and shown again
  const scene = SCENARIOS[at]
  const messages = useMemo(() => { n = 0; return scene.messages() }, [scene])
  const crew = [MASTER, ...TEAM].map(({ id, name }) => ({ id, name }))
  const noop = () => {}
  return (
    <div style={{ display: "grid", gridTemplateColumns: "240px 1fr", height: "100vh", background: "#000" }}>
      <nav style={{ padding: 16, borderRight: "1px solid #27272a", overflow: "auto" }}>
        <h1 style={{ font: "500 0.7rem var(--mono)", letterSpacing: "0.12em", textTransform: "uppercase", color: "#71717a" }}>Fixtures</h1>
        {SCENARIOS.map((s, i) => (
          <button key={s.name} onClick={() => setAt(i)} style={{ display: "block", width: "100%", margin: "2px 0", padding: "6px 8px", border: 0, borderRadius: 6, textAlign: "left", cursor: "pointer", font: "inherit", fontSize: "0.85rem", background: i === at ? "#18181b" : "none", color: i === at ? "#fff" : "#a1a1aa" }}>{s.name}</button>
        ))}
        <p style={{ marginTop: 16, fontSize: "0.72rem", color: "#52525b" }}>Question and approval cards look the task up on the server, so they show as answered unless a task with that id exists.</p>
      </nav>
      <ChatView key={at} agent={MASTER} messages={messages} streaming={Boolean(scene.streaming)} agentTyping={scene.typing}
        onSend={noop} onClearMemory={noop} onOpenProfile={noop} statusMsg="" peers={TEAM.map(({ id, name }) => ({ id, name }))} crew={crew}
        asking={(scene.asking ?? []).map((id) => crew.find((c) => c.id === id)!)} onForward={noop} onSelectPeer={noop} peerThread={null}
        onClosePeer={noop} onOpenTeam={noop} teamOpen={false} />
      {scene.offer && <PaymentsOffer key={at} projectId="fixture-offer" onOpenWallet={noop} />}
    </div>
  )
}
