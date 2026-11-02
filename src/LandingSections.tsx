import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react"
import { EDGES } from "./NeuronLoader"
import { Split } from "./LandingMotion"

// Seven moments of trust. Each panel recreates the product screen it describes (plain markup, not a screenshot), so the claim is shown, not told.
const row = (k: string, v: string) => <div className="lp-art-row"><span>{k}</span><b>{v}</b></div>
const FLOW: { title: string; text: string; art: ReactNode }[] = [
  { title: "Plan, then build", text: "The Master bot writes the first-build plan as a document. Nothing is built until you approve it.",
    art: (<div className="lp-art"><div className="lp-art-bar">The Wick &amp; Wax first-build plan is ready. Build it?</div>
      <ul className="lp-art-list"><li>Muse writes the code in a sandbox and runs it to check it works.</li><li>You get a run guide, the code saved in Docs, and a pull request waiting for your Allow.</li></ul>
      <div className="lp-art-actions"><span className="lp-btn">Read the plan ↗</span><span className="lp-btn is-primary">Approve and build</span></div></div>) },
  { title: "A live preview while it builds", text: "The build streams a live preview, and the team already plans the next phase with you.",
    art: (<div className="lp-art lp-art-shop"><div className="lp-art-bar"><i className="lp-dot" />Live preview</div><img src="/demo-site/images/hero.webp" alt="" width="640" height="400" loading="lazy" /></div>) },
  { title: "Approval before anything leaves", text: "Sending mail, publishing a site or adding a routine waits behind an Allow or Deny card.",
    art: (<div className="lp-art"><div className="lp-art-bar">Orion wants to do this as you. Allow it?</div>
      <div className="lp-art-body">{row("Publish", "the Wick & Wax shop at /sites/wick-wax")}</div>
      <div className="lp-art-actions"><span className="lp-btn">Deny</span><span className="lp-btn">Allow in this session</span><span className="lp-btn is-primary">Allow</span></div></div>) },
  { title: "Memory with versions", text: "A bot's notes are saved as drafts. Promote, reject or roll back each version from its profile.",
    art: (<div className="lp-art"><div className="lp-art-bar">Amber · memory</div>
      <div className="lp-art-body"><div className="lp-art-ver"><em>draft</em><span>Shipping is flat ₹79, free over ₹1,499.</span></div><div className="lp-art-ver is-live"><em>live</em><span>Prices are in whole rupees, with the ₹ sign first.</span></div></div>
      <div className="lp-art-actions"><span className="lp-btn">Reject</span><span className="lp-btn">Roll back</span><span className="lp-btn is-primary">Promote</span></div></div>) },
  { title: "Sites that get paid", text: "Publish a site your bots built. Orders from its form wake the order routines, and payments land in your wallet.",
    art: (<div className="lp-art"><div className="lp-art-bar">Payment received · ₹1,277.00</div>
      <div className="lp-art-body">{row("Order", "Monsoon Jasmine, Sandalwood Dusk")}{row("Fees", "− ₹63.85")}{row("You receive", "₹1,213.15")}</div></div>) },
  { title: "Voice calls with a bot", text: "Talk to a bot out loud. Usage is tracked and billed, so nothing is a surprise.",
    art: (<div className="lp-art"><div className="lp-art-bar">Voice call with Amber</div>
      <div className="lp-art-body"><div className="lp-art-line"><b>You</b>Is everything in stock?</div><div className="lp-art-line"><b>Amber</b>Four are. Gulab Oud is out of stock, so it can&rsquo;t be ordered.</div></div></div>) },
  { title: "Pull request review", text: "Bots review pull requests, optionally in an isolated sandbox, and can write a wiki for a repo.",
    art: (<div className="lp-art"><div className="lp-art-bar">Review · 2 findings</div>
      <div className="lp-art-body"><div className="lp-art-line"><b>cart.js</b>A quantity can go below zero.</div><div className="lp-art-line"><b>shop.test</b>Nothing pins the free-shipping threshold.</div></div></div>) },
]

export function Flow() {
  return (
    <section className="lp-flow" id="new" aria-labelledby="flow-title">
      <header className="lp-flow-head">
        <p className="lp-eyebrow">New on Neuron</p>
        <h2 id="flow-title" data-split><Split lines={["Built to be *trusted*", "with real work"]} /></h2>
      </header>
      <div className="lp-stack">
        <ol className="lp-moments">
          {FLOW.map(({ title, text, art }, i) => (
            <li className="lp-moment" key={title}>
              <i className="lp-moment-rule" aria-hidden="true" />
              <span className="lp-moment-num" aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>
              <h3><Split lines={[title]} /></h3>
              <p>{text}</p>
              <div className="lp-moment-art" aria-hidden="true">{art}</div>
            </li>
          ))}
        </ol>
        <div className="lp-flow-stage" aria-hidden="true">
          <span className="lp-flow-ghost">01</span>
          {FLOW.map(({ title, art }) => <div className="lp-screen" key={title}>{art}</div>)}
          <p className="lp-stage-count"><b>01</b> / {String(FLOW.length).padStart(2, "0")}</p>
        </div>
      </div>
      <nav className="lp-flow-index" aria-label="Moments">
        {FLOW.map(({ title }, i) => (
          <button key={title} type="button" className={i === 0 ? "is-on" : ""} onClick={(e) => e.currentTarget.closest(".lp-flow")?.dispatchEvent(new CustomEvent("lp-flow-goto", { detail: i }))}>
            <span aria-hidden="true">{String(i + 1).padStart(2, "0")}</span>{title}
          </button>
        ))}
      </nav>
    </section>
  )
}

// The loader's wireframe N, large: every line draws itself once, in sequence, and then it stays complete.
export function NeuronMark() {
  return (
    <div className="lp-mark" role="img" aria-label="Neuron">
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <g className="wire-base" strokeWidth="0.9">{EDGES.map(([a, b, c, d]) => <line key={`b${a}${b}${c}${d}`} x1={a} y1={b} x2={c} y2={d} />)}</g>
        <g className="wire-edge" strokeWidth="1.1">{EDGES.map(([a, b, c, d, delay]) => <line key={`e${a}${b}${c}${d}`} x1={a} y1={b} x2={c} y2={d} style={{ animationDelay: `${0.2 + delay * 1.3}s` }} />)}</g>
      </svg>
    </div>
  )
}

// Everything around the story, as a periodic table: a symbol, a number, a family. Hover or focus a tile and the readout explains it.
type Family = "Build" | "Run" | "Trust" | "Team" | "Connect"
const FAMILIES: { name: Family; hue: string }[] = [
  { name: "Build", hue: "#8b7bff" }, { name: "Run", hue: "#2dd4bf" }, { name: "Trust", hue: "#f5b942" }, { name: "Team", hue: "#f472b6" }, { name: "Connect", hue: "#38bdf8" },
]
const ELEMENTS: { sym: string; name: string; family: Family; text: string }[] = [
  { sym: "Pl", name: "Plan, then build", family: "Build", text: "The Master bot writes the first-build plan as a document. Nothing is built until you approve it." },
  { sym: "Bu", name: "Sandbox build", family: "Build", text: "Muse builds in a sandbox and hands back the code, a run guide and a QA report." },
  { sym: "Pv", name: "Live preview", family: "Build", text: "Run the build live in a docked, resizable preview, with a phone-width view to check it is responsive." },
  { sym: "Im", name: "Images", family: "Build", text: "Muse can generate images for the app it is building." },
  { sym: "Si", name: "Hosted sites", family: "Build", text: "Publish a static site under /sites/your-name. It is served sandboxed, rate limited and size capped." },
  { sym: "Pr", name: "PR review", family: "Build", text: "Bots review pull requests, optionally in an isolated sandbox, with a walkthrough and line-by-line findings." },
  { sym: "Wi", name: "Repo wiki", family: "Build", text: "A bot can write a wiki for a repository and keep it up to date." },
  { sym: "Or", name: "Orders", family: "Run", text: "A form on your site wakes your bots or order routines, and an order card appears in the chat." },
  { sym: "Pa", name: "Payments", family: "Run", text: "Hosted Razorpay checkout with prices set in your Wallet. 3% to Neuron plus about 2% to the gateway, paid out after a 7-day refund period." },
  { sym: "Ro", name: "Routines", family: "Run", text: "Give a bot a schedule, or an event like an order, and it runs the task on its own, then reports back." },
  { sym: "Go", name: "Goals", family: "Run", text: "Every bot has one goal, with a map of where it has been and what comes next." },
  { sym: "Ca", name: "Team calendar", family: "Run", text: "Bots read and edit one shared calendar for the whole team." },
  { sym: "Da", name: "Data", family: "Run", text: "Orders and other records your site collects live in one place, where bots can read and update them." },
  { sym: "Al", name: "Allow or deny", family: "Trust", text: "Sending mail, publishing a site or adding a routine waits behind an approval card. Nothing runs until a person allows it." },
  { sym: "Me", name: "Versioned memory", family: "Trust", text: "A bot's notes are saved as drafts. Promote, reject or roll back each version from its profile." },
  { sym: "Ne", name: "Needs you", family: "Trust", text: "Everything waiting on you is pinned above the chat, so nothing sits unseen." },
  { sym: "Pt", name: "Path", family: "Trust", text: "A rail that tracks Plan, Build, Live, Orders and Payments, and what each stage cost." },
  { sym: "Vo", name: "Voice calls", family: "Trust", text: "Talk to a bot out loud. Usage is tracked and billed, so nothing is a surprise." },
  { sym: "Us", name: "Usage", family: "Trust", text: "See your usage against the six-hour and weekly limits of your plan." },
  { sym: "Pe", name: "People", family: "Team", text: "Add people with the field they are expert in. Bots route decisions to the right person and carry on from the answer." },
  { sym: "Fw", name: "Forwarding", family: "Team", text: "Pass any message to another bot or a teammate, with a note on what to do." },
  { sym: "Dm", name: "Direct messages", family: "Team", text: "Message teammates directly, and share a chat or a document with them." },
  { sym: "Do", name: "Documents", family: "Team", text: "Bots write documents (markdown, PDF, spreadsheets) that open in an overlay. Edit one and the bot is told what changed." },
  { sym: "At", name: "Attach files", family: "Team", text: "Drop in a PDF, an image or a code file. The bot reads it and answers from it." },
  { sym: "Gh", name: "GitHub", family: "Connect", text: "Connect a repo so the bots can read the code and review pull requests." },
  { sym: "Ji", name: "Jira", family: "Connect", text: "Bots create and search tickets in your Jira project, and only in that one." },
  { sym: "Cf", name: "Confluence", family: "Connect", text: "Atlassian is connected as a whole: Confluence pages sit beside your Jira tickets." },
  { sym: "Gm", name: "Gmail", family: "Connect", text: "Bots can read and send mail. Sending always waits for your Allow." },
  { sym: "We", name: "Web search", family: "Connect", text: "Bots look things up on their own before they write a plan or a document." },
  { sym: "Pu", name: "Push alerts", family: "Connect", text: "A notification when a bot has something for you, even with the tab closed." },
]

export function Elements() {
  const [active, setActive] = useState(0)
  const [family, setFamily] = useState<Family | "All">("All")
  const hue = (f: Family) => FAMILIES.find((x) => x.name === f)?.hue ?? "#fff"
  const el = ELEMENTS[active]
  const grid = useRef<HTMLDivElement>(null)
  const [phase, setPhase] = useState<"shown" | "armed" | "in">("shown")  // armed: tiles waiting; in: they arrive from the centre outward
  useEffect(() => {
    const node = grid.current
    if (!node || window.matchMedia("(prefers-reduced-motion: reduce)").matches) return
    setPhase("armed")
    const watch = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) { setPhase("in"); watch.disconnect() } }, { threshold: 0.15 })
    watch.observe(node)
    return () => watch.disconnect()
  }, [])
  const pick = (f: Family | "All") => {
    setFamily(f)
    if (f !== "All" && ELEMENTS[active].family !== f) setActive(ELEMENTS.findIndex((x) => x.family === f))
  }
  return (
    <section className="lp-elements" aria-labelledby="elements-title">
      <header className="lp-el-head">
        <p className="lp-eyebrow">Everything around it</p>
        <h2 id="elements-title" data-split><Split lines={[`${ELEMENTS.length} small things,`, "*all included.*"]} /></h2>
      </header>
      <div className="lp-el-body">
        <aside className="lp-readout" aria-live="polite" style={{ "--hue": hue(el.family) } as CSSProperties}>
          <div className="lp-readout-in" key={active}>
            <span className="lp-readout-sym" aria-hidden="true">{el.sym}</span>
            <p className="lp-readout-meta"><i />{el.family} · {String(active + 1).padStart(2, "0")} / {ELEMENTS.length}</p>
            <h3>{el.name}</h3>
            <p className="lp-readout-text">{el.text}</p>
          </div>
        </aside>
        <div className="lp-el-table">
          <div className="lp-el-filters" role="group" aria-label="Filter by family">
            {(["All", ...FAMILIES.map((f) => f.name)] as (Family | "All")[]).map((f) => (
              <button key={f} type="button" aria-pressed={family === f} className={family === f ? "is-on" : ""} onClick={() => pick(f)}>
                {f !== "All" && <i style={{ background: hue(f) }} />}{f}
              </button>
            ))}
          </div>
          <div
            className="lp-el-grid" ref={grid} data-phase={phase}
            onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`); e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`) }}
          >
            {ELEMENTS.map((x, i) => (
              <button
                key={x.sym} type="button" className={`lp-el${i === active ? " is-on" : ""}${family !== "All" && x.family !== family ? " is-dim" : ""}`}
                style={{ "--hue": hue(x.family), "--d": Math.round(Math.hypot((i % 6) - 2.5, Math.floor(i / 6) - 2) * 70) } as CSSProperties} aria-label={`${x.name}. ${x.text}`} aria-current={i === active ? "true" : undefined}
                onPointerEnter={() => setActive(i)} onFocus={() => setActive(i)} onClick={() => setActive(i)}
              >
                <small aria-hidden="true">{String(i + 1).padStart(2, "0")}</small>
                <b aria-hidden="true">{x.sym}</b>
                <span aria-hidden="true">{x.name}</span>
                <i aria-hidden="true" />
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  )
}

const WORDS = ["Plan", "Build", "Approve", "Run", "Review", "Remember"]

// Decorative type strip. Two rows slide against each other as the page scrolls.
export function Marquee() {
  const row = [...WORDS, ...WORDS, ...WORDS].map((w, i) => <span key={i}>{w}<b> / </b></span>)
  return (
    <div className="lp-marquee" aria-hidden="true">
      <div className="lp-marquee-row" data-dir="-1">{row}</div>
      <div className="lp-marquee-row is-solid" data-dir="1">{row}</div>
    </div>
  )
}

export function Final({ onStart }: { onStart: () => void }) {
  return (
    <section className="lp-final" aria-labelledby="final-title">
      <h2 id="final-title" data-split><Split lines={["Put your army", "to work."]} /></h2>
      <button type="button" className="lp-cta-big" onClick={onStart}>Get started</button>
    </section>
  )
}
