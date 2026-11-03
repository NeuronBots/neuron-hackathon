import { useCallback, useEffect, useState } from "react"
import type { Wallet } from "./types"
import { addCheckout, answerRefund, enablePayments, fetchWallet, requestPayout, savePrices } from "./api"
import { money } from "./money"
import { TeamButton } from "./Sidebar"

type Props = { onClose: () => void; onOpenTeam: () => void; teamOpen: boolean }

export function WalletView({ onClose, onOpenTeam, teamOpen }: Props) {
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [error, setError] = useState("")
  const [confirming, setConfirming] = useState("") // a refund sends money back: it takes a second click
  const [busy, setBusy] = useState(false)
  const [adding, setAdding] = useState(false)

  const load = useCallback(() => fetchWallet().then((w) => { setWallet(w); setError("") }, (e) => setError(String(e.message || e))), [])
  useEffect(() => { void load() }, [load])

  const act = async (work: () => Promise<void>) => {
    setBusy(true)
    try { await work(); setConfirming(""); await load() } catch (e) { setError(e instanceof Error ? e.message : "Something went wrong.") } finally { setBusy(false) }
  }
  // the price list, edited in whole units (rupees) and saved in the smallest (paise)
  const [rows, setRows] = useState<{ sku: string; name: string; price: string }[]>([])
  useEffect(() => { if (wallet) setRows(wallet.prices.map((p) => ({ sku: p.sku, name: p.name, price: String(p.amount / 100) }))) }, [wallet])
  const currency = wallet?.prices[0]?.currency ?? "INR"
  const setRow = (i: number, patch: Partial<(typeof rows)[number]>) => setRows((all) => all.map((r, j) => (j === i ? { ...r, ...patch } : r)))
  const saveRows = () => act(() => savePrices(rows.filter((r) => r.sku.trim()).map((r) => ({ sku: r.sku.trim(), name: r.name.trim(), amount: Math.round(Number(r.price) * 100), currency }))))
  const currencies = wallet ? Object.keys(wallet.balance) : []
  const asked = wallet?.payments.filter((p) => p.status === "refund_requested") ?? []
  const canPayOut = currencies.some((c) => (wallet?.balance[c].available ?? 0) > 0)

  return (
    <div className="cal">
      <header className="cal-bar">
        <TeamButton onClick={onOpenTeam} open={teamOpen} />
        <div className="cal-heading">
          <h2 className="wallet-title">Wallet</h2>
          <p className="wallet-subtitle">{wallet ? `${wallet.platform_fee_pct}% Neuron + ~${wallet.gateway_fee_pct}% gateway per payment` : ""}</p>
        </div>
        <button className="wallet-payout-btn" type="button" disabled={!canPayOut || busy} onClick={() => act(requestPayout)}>
          <span>Request payout</span><b aria-hidden="true">→</b>
        </button>
        <button className="cal-today" onClick={onClose} type="button">← Chat</button>
      </header>
      <div className="data-body">
        {error && <p className="data-empty">{error}</p>}
        {wallet && !wallet.enabled && (
          <div className="wallet-payments-intro">
            <h2 className="src-run-head is-running pay-offer-head">
              <span className="pay-offer-kicker">New</span>
              <span>Introducing Payments</span>
            </h2>
            <p className="pay-offer-lead">Let customers pay on your site. The money lands in your Wallet.</p>
            {error && <p className="src-approval-error">{error}</p>}
            <div className="dm-share-actions">
              {wallet.offer && (
                <button type="button" className="dm-share-no" onClick={() => window.dispatchEvent(new Event("neuron:payments-offer"))}>Learn more</button>
              )}
              <button type="button" className="dm-share-yes" disabled={busy} onClick={() => act(enablePayments)}>Turn on payments</button>
            </div>
          </div>
        )}
        {wallet?.enabled && (
          <p className="wallet-notice">
            Muse adds checkout to your site in the coding session, then saves it and asks you to allow publishing it. Set your prices below first.{" "}
            <button type="button" className="btn" disabled={busy || adding} onClick={() => act(async () => { await addCheckout(); setAdding(true) })}>{adding ? "Muse is adding checkout…" : "Add checkout to my site"}</button>
          </p>
        )}
        {wallet?.enabled && (
          <>
            <div className="wallet-cards">
              {(currencies.length ? currencies : ["INR"]).map((c) => {
                const b = wallet.balance[c] ?? { pending: 0, available: 0, paid_out: 0, refunded: 0 }
                return (
                  <div key={c} className="wallet-card-set">
                    <div className="wallet-bal-card wallet-bal-card--highlight"
                      onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`); e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`) }}>
                      <span className="wallet-bal-label">Available to pay out</span>
                      <strong className="wallet-bal-amount">{money(b.available, c)}</strong>
                    </div>
                    <div className="wallet-bal-card"
                      onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`); e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`) }}>
                      <span className="wallet-bal-label">Pending · {wallet.refund_days}-day refund period</span>
                      <strong className="wallet-bal-amount">{money(b.pending, c)}</strong>
                    </div>
                    <div className="wallet-bal-card"
                      onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`); e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`) }}>
                      <span className="wallet-bal-label">Paid out</span>
                      <strong className="wallet-bal-amount">{money(b.paid_out, c)}</strong>
                    </div>
                    <div className="wallet-bal-card"
                      onPointerMove={(e) => { const r = e.currentTarget.getBoundingClientRect(); e.currentTarget.style.setProperty("--mx", `${e.clientX - r.left}px`); e.currentTarget.style.setProperty("--my", `${e.clientY - r.top}px`) }}>
                      <span className="wallet-bal-label">Refunded</span>
                      <strong className="wallet-bal-amount">{money(b.refunded, c)}</strong>
                    </div>
                  </div>
                )
              })}
            </div>
            {asked.length > 0 && (
              <section className="wallet-section">
                <p className="wallet-eyebrow">Refund requests</p>
                {asked.map((p) => (
                  <div key={p.id} className="wallet-refund">
                    <span><strong>{money(p.amount, p.currency)}</strong> · {p.refund_reason || "No reason given"}</span>
                    {confirming === p.id
                      ? <button type="button" className="btn btn-primary" disabled={busy} onClick={() => act(() => answerRefund(p.id, true))}>Confirm: send {money(p.amount, p.currency)} back</button>
                      : <button type="button" className="btn btn-primary" disabled={busy} onClick={() => setConfirming(p.id)}>Approve refund</button>}
                    <button type="button" className="btn" disabled={busy} onClick={() => act(() => answerRefund(p.id, false))}>Decline</button>
                  </div>
                ))}
              </section>
            )}
            <section className="wallet-section">
              <p className="wallet-eyebrow">Prices</p>
              {wallet.proposed_prices.length > 0 && (
                <div className="wallet-proposal">
                  <div className="wallet-proposal-body">
                    <p className="wallet-proposal-title">Muse found {wallet.proposed_prices.length} price{wallet.proposed_prices.length === 1 ? "" : "s"} on your site</p>
                    <p className="wallet-proposal-items">{wallet.proposed_prices.map((p) => `${p.name} · ${money(p.amount, p.currency)}`).join("  /  ")}</p>
                    <p className="wallet-proposal-hint">Only a suggestion — nobody is charged until you save.</p>
                  </div>
                  <button type="button" className="wallet-proposal-btn" onClick={() => setRows(wallet.proposed_prices.map((p) => ({ sku: p.sku, name: p.name, price: String(p.amount / 100) })))}>
                    Use these prices <b aria-hidden="true">→</b>
                  </button>
                </div>
              )}
              <p className="wallet-notice">Your site charges customers by SKU code — the amount always comes from this list.</p>
              <div className="wallet-prices">
                <div className="wallet-prices-head" aria-hidden="true">
                  <span>Code</span><span>Name</span><span>Price ({currency})</span><span />
                </div>
                {rows.map((r, i) => (
                  <div key={i} className="wallet-price-row">
                    <input aria-label="Code" placeholder="e.g. drop-in" value={r.sku} onChange={(e) => setRow(i, { sku: e.target.value.toLowerCase() })} />
                    <input aria-label="Name" placeholder="Display name" value={r.name} onChange={(e) => setRow(i, { name: e.target.value })} />
                    <input aria-label={`Price in ${currency}`} placeholder="0" inputMode="decimal" value={r.price} onChange={(e) => setRow(i, { price: e.target.value })} />
                    <button type="button" className="wallet-remove-btn" aria-label="Remove" onClick={() => setRows((all) => all.filter((_, j) => j !== i))}>×</button>
                  </div>
                ))}
                <div className="wallet-price-actions">
                  <button type="button" className="btn" onClick={() => setRows((all) => [...all, { sku: "", name: "", price: "" }])}>+ Add item</button>
                  <button type="button" className="wallet-save-btn" disabled={busy} onClick={saveRows}>
                    <span>Save prices</span><b aria-hidden="true">→</b>
                  </button>
                </div>
              </div>
            </section>
            <section className="wallet-section">
              <p className="wallet-eyebrow">Payments</p>
              {wallet.payments.length === 0
                ? <p className="data-empty">No payments yet. They appear here when customers pay on your site.</p>
                : (
                  <div className="wallet-table-wrap">
                    <table className="wallet-table">
                      <thead>
                        <tr>
                          <th>Received</th>
                          <th className="wallet-th-num">Amount</th>
                          <th className="wallet-th-num">Fees</th>
                          <th className="wallet-th-num">You get</th>
                          <th>Status</th>
                        </tr>
                      </thead>
                      <tbody>
                        {wallet.payments.map((p) => (
                          <tr key={p.id}>
                            <td className="wallet-td-date">{new Date(p.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</td>
                            <td className="wallet-td-num">{money(p.amount, p.currency)}</td>
                            <td className="wallet-td-num wallet-td-muted">{money(p.platform_fee + p.gateway_fee, p.currency)}</td>
                            <td className="wallet-td-num wallet-td-net">{money(p.net, p.currency)}</td>
                            <td><span className={`wallet-status${p.test ? " is-test" : ""}`}>{p.test ? "test" : p.status.replace(/_/g, " ")}</span></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
            </section>
            {wallet.payouts.length > 0 && (
              <section className="wallet-section">
                <p className="wallet-eyebrow">Payouts</p>
                {wallet.payouts.map((o) => (
                  <p key={o.id} className="wallet-notice">{money(o.amount, o.currency)} · {o.status} · {new Date(o.created_at).toLocaleDateString()}</p>
                ))}
              </section>
            )}
          </>
        )}
      </div>
    </div>
  )
}
