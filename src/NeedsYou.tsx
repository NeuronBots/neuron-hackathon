import { useEffect, useState } from "react"
import { fetchTasks } from "./api"
import type { TaskRead } from "./types"

const REFRESH_MS = 20_000  // a bot in another chat can ask for you while you read this one

// Everything in the project that waits on the person, pinned above the composer: the newest named, the rest on demand.
export function NeedsYou({ here, crew, refresh, onSelectAgent }: {
  here: string
  crew: { id: string; name: string }[]
  refresh: string
  onSelectAgent: (id: string) => void
}) {
  const [tasks, setTasks] = useState<TaskRead[]>([])
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let live = true
    const load = () => fetchTasks({ status: "decision" }).then((found) => { if (live) setTasks(found) }).catch(() => {})
    void load()
    const timer = setInterval(load, REFRESH_MS)
    return () => { live = false; clearInterval(timer) }
  }, [refresh])

  if (tasks.length === 0) return null
  const items = [...tasks].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""))
  const who = (id: string) => crew.find((c) => c.id === id)?.name ?? ""

  const jump = (task: TaskRead) => {
    setOpen(false)
    if (task.agent_id !== here) { onSelectAgent(task.agent_id); return }  // its card is at the end of that chat: waiting cards sink to the bottom
    const card = document.getElementById(`ask-${task.id}`)
    if (!card) return
    card.scrollIntoView({ block: "center", behavior: "smooth" })
    card.classList.add("is-called")
    window.setTimeout(() => card.classList.remove("is-called"), 1400)
  }

  return (
    <div className="needs-you" role="status">
      <button type="button" className="needs-head" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <span className="needs-dot" aria-hidden="true" />
        <span className="needs-label">Needs you · {items.length}</span>
        <span className="needs-now">{items[0].title}{who(items[0].agent_id) && <i> {who(items[0].agent_id)}</i>}</span>
        <span className="needs-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul className="needs-list">
          {items.map((task) => (
            <li key={task.id}>
              <button type="button" onClick={() => jump(task)}>
                <span className="needs-who">{who(task.agent_id) || "Bot"}</span>
                <span className="needs-title">{task.title}</span>
                <span className="needs-go" aria-hidden="true">{task.agent_id === here ? "↓" : "→"}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
