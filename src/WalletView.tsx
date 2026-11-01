import { useCallback, useEffect, useState } from "react"
import type { Wallet } from "./types"
import { addCheckout, answerRefund, fetchWallet, requestPayout, savePrices } from "./api"
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
          <h2 className="cal-title">Wallet</h2>
          <p className="cal-range">{wallet ? `${wallet.platform_fee_pct}% Neuron + about ${wallet.gateway_fee_pct}% payment gateway on each payment` : ""}</p>
        </div>
        <button className="btn btn-primary cal-add" type="button" disabled={!canPayOut || busy} onClick={() => act(requestPayout)}>Request payout</button>
        <button className="cal-today" onClick={onClose} type="button">← Chat</button>
      </header>
      <div className="data-body">
        {error && <p className="data-empty">{error}</p>}
        {wallet && !wallet.enabled && (
          <p className="data-empty">
            Payments are not turned on for this project yet.
            {wallet.offer && <> <button type="button" className="btn btn-primary" onClick={() => window.dispatchEvent(new Event("neuron:payments-offer"))}>See how payments work</button></>}
          </p>
        )}
        {wallet?.enabled && (
          <p className="wallet-line">
            Muse adds checkout to your site in the coding session, then saves it and asks you to allow publishing it. Set your prices below first, so the page has something to sell.{" "}
            <button type="button" className="btn" disabled={busy || adding} onClick={() => act(async () => { await addCheckout(); setAdding(true) })}>{adding ? "Muse is adding checkout…" : "Add checkout to my site"}</button>
          </p>
        )}
        {wallet && (
          <>
            <div className="wallet-cards">
              {(currencies.length ? currencies : ["INR"]).map((c) => {
                const b = wallet.balance[c] ?? { pending: 0, available: 0, paid_out: 0, refunded: 0 }
                return (
                  <div key={c} className="wallet-card-set">
                    <div className="wallet-card"><span>Available to pay out</span><strong>{money(b.available, c)}</strong></div>
                    <div className="wallet-card"><span>Pending ({wallet.refund_days}-day refund period)</span><strong>{money(b.pending, c)}</strong></div>
                    <div className="wallet-card"><span>Paid out</span><strong>{money(b.paid_out, c)}</strong></div>
                    <div className="wallet-card"><span>Refunded</span><strong>{money(b.refunded, c)}</strong></div>
                  </div>
                )
              })}
            </div>
            {asked.length > 0 && (
              <section>
                <h3 className="wallet-h">Refund requests</h3>
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
            <h3 className="wallet-h">Prices</h3>
            {wallet.proposed_prices.length > 0 && (
              <p className="wallet-line">
                Muse found {wallet.proposed_prices.length} price{wallet.proposed_prices.length === 1 ? "" : "s"} on your site: {wallet.proposed_prices.map((p) => `${p.name} ${money(p.amount, p.currency)}`).join(", ")}.{" "}
                <button type="button" className="btn" onClick={() => setRows(wallet.proposed_prices.map((p) => ({ sku: p.sku, name: p.name, price: String(p.amount / 100) })))}>Use these prices</button>{" "}
                They are only a suggestion: nobody is charged anything until you press Save prices.
              </p>
            )}
            <p className="wallet-line">What customers are charged. Your site names an item by its code; the amount always comes from this list.</p>
            {rows.map((r, i) => (
              <div key={i} className="wallet-price">
                <input aria-label="Code" placeholder="code, e.g. drop-in" value={r.sku} onChange={(e) => setRow(i, { sku: e.target.value.toLowerCase() })} />
                <input aria-label="Name" placeholder="Name" value={r.name} onChange={(e) => setRow(i, { name: e.target.value })} />
                <input aria-label={`Price in ${currency}`} placeholder={`Price (${currency})`} inputMode="decimal" value={r.price} onChange={(e) => setRow(i, { price: e.target.value })} />
                <button type="button" className="btn" onClick={() => setRows((all) => all.filter((_, j) => j !== i))}>Remove</button>
              </div>
            ))}
            <div className="wallet-refund">
              <button type="button" className="btn" onClick={() => setRows((all) => [...all, { sku: "", name: "", price: "" }])}>Add item</button>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={saveRows}>Save prices</button>
            </div>
            <h3 className="wallet-h">Payments</h3>
            {wallet.payments.length === 0
              ? <p className="data-empty">No payments yet. They show up here when customers pay on your site.</p>
              : (
                <table className="data-table">
                  <thead><tr><th>Received</th><th>Amount</th><th>Fees</th><th>You get</th><th>Status</th></tr></thead>
                  <tbody>
                    {wallet.payments.map((p) => (
                      <tr key={p.id}>
                        <td>{new Date(p.created_at).toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}</td>
                        <td>{money(p.amount, p.currency)}</td>
                        <td>{money(p.platform_fee + p.gateway_fee, p.currency)}</td>
                        <td>{money(p.net, p.currency)}</td>
                        <td><span className={`data-status${p.test ? " is-test" : ""}`}>{p.test ? "test" : p.status.replace("_", " ")}</span></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            {wallet.payouts.length > 0 && (
              <>
                <h3 className="wallet-h">Payouts</h3>
                {wallet.payouts.map((o) => (
                  <p key={o.id} className="wallet-line">{money(o.amount, o.currency)} · {o.status} · {new Date(o.created_at).toLocaleDateString()}</p>
                ))}
              </>
            )}
          </>
        )}
      </div>
    </div>
  )
}
