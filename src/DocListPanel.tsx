import { useEffect, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { fetchDocs, fetchDocById, shareDoc, deleteDoc, deleteDocumentFile, sendDirect, type DocItem } from "./api"
import type { ProjectMember } from "./types"

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—"
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return "—"
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  })
}

export interface DocListPanelProps {
  onClose?: () => void
  selectedDocId?: string | null
  onSelectDoc?: (doc: DocItem | null) => void
  members?: ProjectMember[]
  className?: string
}

export function DocListPanel({
  onClose,
  selectedDocId: externalSelectedId,
  onSelectDoc,
  members = [],
  className = "",
}: DocListPanelProps) {
  const [docs, setDocs] = useState<DocItem[] | null>(null)
  const [loadingList, setLoadingList] = useState(false)
  const [listError, setListError] = useState<string | null>(null)

  const [selectedId, setSelectedId] = useState<string | null>(externalSelectedId ?? null)
  const [selectedDoc, setSelectedDoc] = useState<DocItem | null>(null)
  const [loadingDoc, setLoadingDoc] = useState(false)
  const [docError, setDocError] = useState<string | null>(null)

  // Share state
  const [shareOpen, setShareOpen] = useState(false)
  const [shareTarget, setShareTarget] = useState("")
  const [shareNote, setShareNote] = useState("")
  const [shareStatus, setShareStatus] = useState("")
  const [shareSending, setShareSending] = useState(false)
  const [deleteModalOpen, setDeleteModalOpen] = useState(false)
  const [deletingDoc, setDeletingDoc] = useState(false)
  const [deleteDocError, setDeleteDocError] = useState("")

  useEffect(() => {
    if (externalSelectedId !== undefined) {
      setSelectedId(externalSelectedId)
    }
  }, [externalSelectedId])

  function loadDocs() {
    setLoadingList(true)
    setListError(null)
    return fetchDocs()
      .then((data) => {
        const sorted = [...data].sort((a, b) => {
          const timeA = new Date(a.updated_at || a.created_at).getTime()
          const timeB = new Date(b.updated_at || b.created_at).getTime()
          return timeB - timeA
        })
        setDocs(sorted)
      })
      .catch((err) => {
        setDocs([])
        setListError(err instanceof Error ? err.message : "Failed to load documents")
      })
      .finally(() => {
        setLoadingList(false)
      })
  }

  function loadDoc(id: string) {
    setLoadingDoc(true)
    setDocError(null)
    setShareOpen(false)
    setShareStatus("")
    return fetchDocById(id)
      .then((doc) => {
        setSelectedDoc(doc)
        onSelectDoc?.(doc)
      })
      .catch((err) => {
        setSelectedDoc(null)
        setDocError(err instanceof Error ? err.message : "Failed to load document")
      })
      .finally(() => {
        setLoadingDoc(false)
      })
  }

  useEffect(() => {
    void loadDocs()
  }, [])

  function handleSelectDoc(id: string) {
    setSelectedId(id)
    void loadDoc(id)
  }

  function handleBackToList() {
    setSelectedId(null)
    setSelectedDoc(null)
    setDocError(null)
    setShareOpen(false)
    setShareStatus("")
    onSelectDoc?.(null)
  }

  async function handleShareDoc(e: React.FormEvent) {
    e.preventDefault()
    if (!selectedDoc || !shareTarget) return
    setShareSending(true)
    setShareStatus("")
    try {
      const targetUser = members.find((m) => m.id === shareTarget || m.email === shareTarget)
      const targetId = targetUser ? targetUser.id : shareTarget
      const targetEmail = targetUser ? targetUser.email : shareTarget

      // 1. Share the document with the target user
      const usersToShare = [targetId, targetEmail].filter(Boolean)
      const updated = await shareDoc(selectedDoc.id, usersToShare)
      setSelectedDoc(updated)

      // 2. If it's a project member, also send a direct notification message
      if (targetUser) {
        const docLink = `/api/documents/${selectedDoc.id}`
        const msgText = `${shareNote ? shareNote + "\n\n" : ""}[${selectedDoc.title}](${docLink})`
        await sendDirect(targetUser.id, msgText).catch(() => {})
      }

      setShareStatus(`Successfully shared with ${targetUser?.name || shareTarget}`)
      setShareTarget("")
      setShareNote("")
      window.setTimeout(() => setShareOpen(false), 1500)
    } catch (err) {
      setShareStatus(`Failed to share: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setShareSending(false)
    }
  }

  const containerClasses = [
    "doc-panel-container",
    selectedId ? "has-selection" : "",
    className,
  ].filter(Boolean).join(" ")

  async function confirmDeleteDoc() {
    if (!selectedDoc) return
    setDeletingDoc(true)
    setDeleteDocError("")
    try {
      if (selectedDoc.id.includes(".")) {
        await deleteDocumentFile(selectedDoc.id)
      } else {
        await deleteDoc(selectedDoc.id)
      }
      setDocs((prev) => (prev ? prev.filter((d) => d.id !== selectedDoc.id) : []))
      setSelectedDoc(null)
      setDeleteModalOpen(false)
    } catch (err) {
      setDeleteDocError(err instanceof Error ? err.message : "Could not delete document.")
    } finally {
      setDeletingDoc(false)
    }
  }

  return (
    <div className={containerClasses}>
      {/* Doc List Panel */}
      <section className="doc-list-pane" aria-label="Document list">
        <header className="doc-list-header">
          <div className="doc-list-title-wrap">
            <h2 className="doc-list-title">Documents</h2>
            {docs !== null && !loadingList && (
              <span className="doc-list-count">
                ({docs.length} {docs.length === 1 ? "document" : "documents"})
              </span>
            )}
          </div>
          <div style={{ display: "flex", gap: "6px" }}>
            <button
              className="btn btn-quiet btn-sm"
              type="button"
              title="Refresh document list"
              onClick={() => void loadDocs()}
              disabled={loadingList}
            >
              ↻ Refresh
            </button>
            {onClose && (
              <button className="btn btn-quiet btn-sm" type="button" onClick={onClose}>
                Close
              </button>
            )}
          </div>
        </header>

        {loadingList && docs === null && (
          <div className="doc-status-box doc-loading" role="status">
            <p>Loading documents…</p>
          </div>
        )}

        {listError && (
          <div className="doc-status-box doc-error" role="alert">
            <p>Could not load documents: {listError}</p>
            <button className="btn btn-primary btn-sm" type="button" onClick={() => void loadDocs()}>
              Retry
            </button>
          </div>
        )}

        {!loadingList && !listError && docs && docs.length === 0 && (
          <div className="doc-status-box doc-empty">
            <p>No documents found.</p>
          </div>
        )}

        {docs && docs.length > 0 && (
          <div className="doc-list-items" role="list">
            {docs.map((doc) => {
              const isSelected = selectedId === doc.id
              return (
                <div
                  key={doc.id}
                  className={`doc-list-item ${isSelected ? "is-selected" : ""}`}
                  role="button"
                  tabIndex={0}
                  aria-pressed={isSelected}
                  onClick={() => handleSelectDoc(doc.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault()
                      handleSelectDoc(doc.id)
                    }
                  }}
                >
                  <h3 className="doc-list-item-title">{doc.title}</h3>
                  <div className="doc-list-item-meta">
                    <span className="doc-list-item-date">
                      Updated: {formatTimestamp(doc.updated_at)}
                    </span>
                    {doc.tags && doc.tags.length > 0 && (
                      <span className="doc-tag-pill">
                        #{doc.tags[0]}
                        {doc.tags.length > 1 ? ` +${doc.tags.length - 1}` : ""}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* Open View */}
      {selectedId ? (
        <section className="doc-view-pane" aria-label="Document view">
          {loadingDoc && !selectedDoc && (
            <div className="doc-status-box doc-loading" role="status" style={{ margin: "auto" }}>
              <p>Loading document…</p>
            </div>
          )}

          {docError && (
            <div className="doc-status-box doc-error" role="alert" style={{ margin: "auto" }}>
              <p>Could not load document: {docError}</p>
              <div className="doc-error-actions">
                <button
                  className="btn btn-primary btn-sm"
                  type="button"
                  onClick={() => selectedId && void loadDoc(selectedId)}
                >
                  Retry
                </button>
                <button className="btn btn-quiet btn-sm" type="button" onClick={handleBackToList}>
                  Back to list
                </button>
              </div>
            </div>
          )}

          {selectedDoc && (
            <>
              <header className="doc-open-header">
                <div className="doc-open-top-bar">
                  <button
                    className="doc-back btn btn-quiet btn-sm"
                    type="button"
                    onClick={handleBackToList}
                  >
                    Back to list
                  </button>
                  <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                    {selectedDoc.id.includes(".") && (
                      <a
                        className="btn btn-quiet btn-sm"
                        href={`/api/documents/${encodeURIComponent(selectedDoc.id)}`}
                        download={selectedDoc.id}
                      >
                        Download
                      </a>
                    )}
                    <button
                      className="btn btn-quiet btn-sm"
                      type="button"
                      onClick={() => setShareOpen((v) => !v)}
                    >
                      {shareOpen ? "Cancel share" : "Share"}
                    </button>
                    <button
                      className="btn btn-quiet btn-sm"
                      type="button"
                      style={{ color: "#ff6b6b" }}
                      onClick={() => { setDeleteModalOpen(true); setDeleteDocError(""); }}
                      title="Delete document"
                    >
                      Delete
                    </button>
                  </div>
                </div>

                {shareOpen && (
                  <form onSubmit={(e) => void handleShareDoc(e)} className="doc-share-box" style={{ background: "rgba(0,0,0,0.25)", padding: "12px", borderRadius: "10px", marginTop: "10px", border: "1px solid var(--line)" }}>
                    <p style={{ margin: "0 0 8px", fontSize: "0.85rem", color: "var(--ink)", fontWeight: 600 }}>Share with teammate:</p>
                    {members.length > 0 ? (
                      <select
                        value={shareTarget}
                        onChange={(e) => setShareTarget(e.target.value)}
                        className="task-filter-select"
                        style={{ width: "100%", marginBottom: "8px", padding: "6px" }}
                        required
                      >
                        <option value="">Select a teammate…</option>
                        {members.map((m) => (
                          <option key={m.id} value={m.id}>
                            {m.name} ({m.email})
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        type="text"
                        placeholder="User ID or Email"
                        value={shareTarget}
                        onChange={(e) => setShareTarget(e.target.value)}
                        style={{ width: "100%", marginBottom: "8px", padding: "6px", borderRadius: "6px", border: "1px solid var(--line)", background: "var(--field)", color: "var(--ink)" }}
                        required
                      />
                    )}
                    <input
                      type="text"
                      placeholder="Optional note for the recipient…"
                      value={shareNote}
                      onChange={(e) => setShareNote(e.target.value)}
                      style={{ width: "100%", marginBottom: "8px", padding: "6px", borderRadius: "6px", border: "1px solid var(--line)", background: "var(--field)", color: "var(--ink)" }}
                    />
                    <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
                      <button className="btn btn-primary btn-sm" type="submit" disabled={shareSending || !shareTarget}>
                        {shareSending ? "Sharing…" : "Share"}
                      </button>
                      {shareStatus && (
                        <span style={{ fontSize: "0.82rem", color: shareStatus.startsWith("Failed") ? "var(--danger)" : "var(--pine)" }}>
                          {shareStatus}
                        </span>
                      )}
                    </div>
                  </form>
                )}

                <h1 className="doc-open-title">{selectedDoc.title}</h1>
                <div className="doc-open-timestamps">
                  <span className="doc-timestamp">
                    Created: {formatTimestamp(selectedDoc.created_at)}
                  </span>
                  <span className="doc-timestamp-sep">·</span>
                  <span className="doc-timestamp">
                    Updated: {formatTimestamp(selectedDoc.updated_at)}
                  </span>
                  {selectedDoc.shared_with && selectedDoc.shared_with.length > 0 && (
                    <>
                      <span className="doc-timestamp-sep">·</span>
                      <span className="doc-timestamp">
                        Shared with {selectedDoc.shared_with.length} {selectedDoc.shared_with.length === 1 ? "person" : "people"}
                      </span>
                    </>
                  )}
                </div>
                <div className="doc-open-tags">
                  <span className="doc-tags-label">Tags:</span>
                  {selectedDoc.tags && selectedDoc.tags.length > 0 ? (
                    selectedDoc.tags.map((tag) => (
                      <span key={tag} className="doc-tag">
                        #{tag}
                      </span>
                    ))
                  ) : (
                    <span className="doc-no-tags">None</span>
                  )}
                </div>
              </header>
              <div className="doc-open-content doc-body">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {selectedDoc.content}
                </ReactMarkdown>
              </div>
            </>
          )}
        </section>
      ) : (
        <section className="doc-view-pane" aria-label="Document view placeholder">
          <div className="doc-view-placeholder">
            <p>Select a document from the list to view its contents.</p>
          </div>
        </section>
      )}

      {deleteModalOpen && selectedDoc && (
        <div className="doc-overlay" onClick={() => !deletingDoc && setDeleteModalOpen(false)}>
          <div
            className="doc-col"
            onClick={(e) => e.stopPropagation()}
            style={{
              maxWidth: 420,
              margin: "auto",
              background: "var(--field)",
              padding: 24,
              borderRadius: 16,
              border: "1px solid var(--line)",
              boxShadow: "0 20px 40px rgba(0, 0, 0, 0.4)",
            }}
          >
            <h3 style={{ margin: "0 0 10px", fontFamily: "var(--serif)", color: "var(--ink)", fontSize: "1.2rem" }}>
              Delete Document
            </h3>
            <p style={{ margin: "0 0 16px", fontSize: "0.92rem", color: "var(--soft)", lineHeight: 1.5 }}>
              Are you sure you want to delete <b style={{ color: "var(--ink)" }}>{selectedDoc.title}</b>? This action cannot be undone.
            </p>
            {deleteDocError && (
              <p style={{ margin: "0 0 12px", fontSize: "0.85rem", color: "var(--danger, #ff6b6b)" }}>
                {deleteDocError}
              </p>
            )}
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", alignItems: "center" }}>
              <button
                className="btn btn-quiet btn-sm"
                type="button"
                disabled={deletingDoc}
                onClick={() => {
                  setDeleteModalOpen(false)
                  setDeleteDocError("")
                }}
              >
                Cancel
              </button>
              <button
                className="btn btn-sm btn-primary"
                type="button"
                disabled={deletingDoc}
                style={{ background: "#dc2626", borderColor: "#dc2626", color: "#fff" }}
                onClick={() => void confirmDeleteDoc()}
              >
                {deletingDoc ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}