import { Avatar } from "./Avatar"
import { askingNames } from "./askingNames"

type Person = { id: string; name: string }

const W = 360  // the wires are drawn in a 360-unit box that stretches to the card; the nodes below share the same width equally
const H = 44

// The bot you are talking to at the top, the teammates it asked below: wires run while they talk, and a tick marks who has answered.
export function TeamCard({ lead, done, talking, onOpen }: { lead: Person; done: Person[]; talking: Person[]; onOpen?: (id: string) => void }) {
  const people = [...done.filter((p) => !talking.some((t) => t.id === p.id)), ...talking]
  if (people.length === 0) return null
  const live = talking.length > 0
  const still = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  const x = (i: number) => ((i + 0.5) / people.length) * W
  // an org chart's straight lines: down from the lead, along a bar, down to each teammate
  const wire = (i: number) => (Math.abs(x(i) - W / 2) < 0.5 ? `M ${W / 2} 0 V ${H}` : `M ${W / 2} 0 V ${H / 2} H ${x(i)} V ${H}`)
  const caption = live
    ? `${lead.name} is asking ${askingNames(talking)}`
    : `${lead.name} worked with ${askingNames(people)}`

  return (
    <div className={`team-card${live ? " is-live" : ""}`} role={live ? "status" : "group"} aria-label={caption}>
      <div className={`team-head src-run-head ${live ? "is-running" : "is-done"}`}>
        <span className="team-kicker"><span className="team-pulse" aria-hidden="true" />Team</span>
        <span className="team-caption">{caption}</span>
      </div>
      <div className="team-chart">
        <div className="team-lead">
          <span className="team-node is-lead"><Avatar name={lead.name} id={lead.id} size={30} working={live} /><span className="team-name">{lead.name}</span></span>
        </div>
        <svg className="team-wires" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" shape-rendering="crispEdges" aria-hidden="true">
          {people.map((p, i) => {
            const on = talking.some((t) => t.id === p.id)
            return (
              <g key={p.id}>
                <path d={wire(i)} className={`team-wire${on ? " is-on" : " is-done"}`} />
                {on && !still && <circle r="2.4" className="team-packet"><animateMotion dur="1.3s" repeatCount="indefinite" path={wire(i)} /></circle>}
              </g>
            )
          })}
        </svg>
        <div className="team-row">
          {people.map((p) => {
            const on = talking.some((t) => t.id === p.id)
            return (
              <button key={p.id} type="button" className={`team-node${on ? " is-talking" : " is-done"}`} disabled={!onOpen} onClick={() => onOpen?.(p.id)}
                title={on ? `${p.name} is answering` : `Read what ${lead.name} and ${p.name} said`}>
                <span className="team-face"><Avatar name={p.name} id={p.id} size={30} working={on} />{!on && <span className="team-tick" aria-hidden="true">✓</span>}</span>
                <span className="team-name">{p.name}</span>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}
