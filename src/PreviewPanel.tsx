import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react"
import { getActiveProject, getPreview, restartSession, saveSession, stopPreview, type PreviewState } from "./api"

// The built app, running in a sandbox, docked on the right of the chat. The frame is sandboxed away from Neuron (no same-origin), and its address is
// this server's, not the sandbox's. Other components open it with openPreview().
export function openPreview(preview: PreviewState) {
  window.dispatchEvent(new CustomEvent("neuron:preview", { detail: preview }))
}

const WIDTH_KEY = "neuron.previewWidth"
const MIN_W = 320
const MIN_CHAT = 360 // the chat keeps at least this much room
const maxWidth = () => Math.max(MIN_W, window.innerWidth - MIN_CHAT)
const clampWidth = (w: number) => Math.min(maxWidth(), Math.max(MIN_W, Math.round(w)))

function savedWidth(): number | null {
  try {
    const n = Number(localStorage.getItem(WIDTH_KEY))
    return Number.isFinite(n) && n > 0 ? clampWidth(n) : null
  } catch {
    return null // private window: it still resizes, it just is not remembered
  }
}

const Icon = ({ d }: { d: string }) => <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={d} /></svg>

export function PreviewPanel() {
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [frame, setFrame] = useState(0)
  const [saving, setSaving] = useState("")
  const [restarting, setRestarting] = useState("") // "busy", or what went wrong
  const [width, setWidth] = useState<number | null>(savedWidth) // null: the default, set by the stylesheet
  const drag = useRef<{ x: number; w: number } | null>(null)
  const panel = useRef<HTMLElement | null>(null)
  const [phone, setPhone] = useState(false) // see the app at a phone's width, to check that it is responsive
  const id = preview?.id
  const live = preview?.status === "starting" || preview?.status === "running"

  useEffect(() => {
    const open = (e: Event) => setPreview((e as CustomEvent<PreviewState>).detail)
    window.addEventListener("neuron:preview", open)
    return () => window.removeEventListener("neuron:preview", open)
  }, [])

  useEffect(() => {
    document.body.classList.toggle("preview-open", Boolean(preview))
    return () => document.body.classList.remove("preview-open")
  }, [preview])

  // The chosen width is a CSS variable, so the panel and the room left for the chat always agree
  useEffect(() => {
    if (width) document.documentElement.style.setProperty("--preview-w", `${width}px`)
    else document.documentElement.style.removeProperty("--preview-w")
    return () => { document.documentElement.style.removeProperty("--preview-w") }
  }, [width])

  // A narrower window can no longer hold the saved width: keep the chat's room
  useEffect(() => {
    const fit = () => setWidth((w) => (w && w > maxWidth() ? maxWidth() : w))
    window.addEventListener("resize", fit)
    return () => window.removeEventListener("resize", fit)
  }, [])

  const remember = useCallback((w: number | null) => {
    try {
      if (w) localStorage.setItem(WIDTH_KEY, String(w))
      else localStorage.removeItem(WIDTH_KEY)
    } catch { /* not remembered */ }
  }, [])

  const startDrag = (e: PointerEvent<HTMLDivElement>) => {
    drag.current = { x: e.clientX, w: panel.current?.getBoundingClientRect().width ?? width ?? 560 }
    e.currentTarget.setPointerCapture(e.pointerId)
    document.body.classList.add("preview-resizing")
  }
  const moveDrag = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current) setWidth(clampWidth(drag.current.w + (drag.current.x - e.clientX))) // the panel is on the right: dragging left widens it
  }
  const endDrag = () => {
    if (!drag.current) return
    drag.current = null
    document.body.classList.remove("preview-resizing")
    setWidth((w) => { remember(w); return w })
  }
  const keyResize = (e: KeyboardEvent<HTMLDivElement>) => {
    const now = panel.current?.getBoundingClientRect().width ?? width ?? 560
    const step = e.shiftKey ? 80 : 20
    const next = e.key === "ArrowLeft" ? now + step : e.key === "ArrowRight" ? now - step : e.key === "Home" ? MIN_W : e.key === "End" ? maxWidth() : null
    if (next === null) return
    e.preventDefault()
    const w = clampWidth(next)
    setWidth(w)
    remember(w)
  }

  useEffect(() => {
    if (!id || !live) return
    const timer = window.setInterval(() => {
      getPreview(id).then(setPreview).catch(() => setPreview((p) => (p ? { ...p, status: "stopped", stage: "Stopped" } : p)))
    }, preview?.session ? 1500 : 2500)
    return () => window.clearInterval(timer)
  }, [id, live, preview?.session])

  if (!preview) return null
  const close = () => {
    if (live) void stopPreview(preview.id)
    setPreview(null)
  }
  const ended = preview.status === "stopped" || preview.status === "failed"
  const restart = () => {
    setRestarting("busy")
    restartSession(getActiveProject())
      .then((next) => { setPreview(next); setRestarting("") })
      .catch((e) => setRestarting(e instanceof Error ? e.message : "Could not restart."))
  }
  return (
    <aside className="preview-panel" aria-label="Live preview" ref={panel}>
      <div
        className="preview-resize"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the preview"
        aria-valuemin={MIN_W}
        aria-valuemax={maxWidth()}
        aria-valuenow={Math.round(width ?? panel.current?.getBoundingClientRect().width ?? 560)}
        tabIndex={0}
        onPointerDown={startDrag}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onKeyDown={keyResize}
        onDoubleClick={() => { setWidth(null); remember(null) }}
        title="Drag to resize. Double-click to reset."
      />
      <header className="preview-head">
        <span className={`preview-dot preview-${preview.status}`} aria-hidden="true" />
        <strong>{preview.status === "running" ? (preview.busy ? preview.stage || "Muse is changing the code…" : preview.session ? "Coding session" : "Live preview") : preview.stage || preview.status}</strong>
        {preview.busy && <span className="src-spinner" role="status" aria-label="Muse is working" />}
        <span className="preview-spacer" />
        {preview.session && preview.status === "running" && (
          <button type="button" className="preview-icon" aria-label={saving === "busy" ? "Saving" : saving === "done" ? "Saved" : "Save"} title={saving === "done" ? "Saved" : "Save"} disabled={Boolean(saving) && saving !== "done"} onClick={() => {
            setSaving("busy")
            saveSession(preview.id).then(() => setSaving("done")).catch((e) => setSaving(e instanceof Error ? e.message : "Could not save."))
          }}>{saving === "busy" ? <span className="src-spinner" aria-hidden="true" /> : <Icon d={saving === "done" ? "M5 12.5l4.5 4.5L19 7.5" : "M5 4h11l3 3v13H5zM8 4v5h7V4M8 20v-6h8v6"} />}</button>
        )}
        {preview.status === "running" && (
          <button type="button" className="preview-icon" aria-pressed={phone} aria-label={phone ? "Full width" : "Phone width"} title={phone ? "Full width" : "Show the app at a phone's width"} onClick={() => setPhone((on) => !on)}>
            <Icon d={phone ? "M3 5h18v12H3zM8 21h8M12 17v4" : "M8 3h8a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H8a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1zM11 18h2"} />
          </button>
        )}
        {preview.status === "running" && <button type="button" className="preview-icon" aria-label="Reload" title="Reload" onClick={() => setFrame((n) => n + 1)}><Icon d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7" /></button>}
        <button type="button" className="preview-icon" onClick={close} aria-label={live ? "Stop the session" : "Close the preview"} title={live ? "Stop" : "Close"}><Icon d={live ? "M7 7h10v10H7z" : "M6 6l12 12M18 6L6 18"} /></button>
      </header>
      {preview.status === "running" && preview.src ? (
        <div className={`preview-stage${phone ? " is-phone" : ""}`}>
          <iframe key={`${frame}-${preview.version ?? 0}`} className="preview-frame" title="Live preview" src={preview.src} sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox" referrerPolicy="no-referrer" />
        </div>
      ) : (
        <div className="preview-wait">
          {preview.status === "starting" && <span className="src-spinner" role="status" aria-label="Starting" />}
          <p>{preview.status === "starting" ? `${preview.stage}…` : preview.stage}</p>
          {ended && (
            <div className="preview-actions">
              <button type="button" className="btn btn-primary" disabled={restarting === "busy"} onClick={restart}>{restarting === "busy" ? "Restarting…" : "Restart"}</button>
              <button type="button" className="btn" onClick={close}>Close</button>
            </div>
          )}
          {restarting && restarting !== "busy" && <p className="preview-note">{restarting}</p>}
        </div>
      )}
      {saving && saving !== "busy" && saving !== "done" && <p className="preview-note">{saving}</p>}
      {preview.logs.length > 0 && (
        <details className="preview-logs" open={preview.status === "failed"}>
          <summary>Log</summary>
          <pre>{preview.logs.join("\n")}</pre>
        </details>
      )}
    </aside>
  )
}
