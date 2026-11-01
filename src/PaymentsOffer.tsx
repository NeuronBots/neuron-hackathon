import { useEffect, useState } from "react"
import type { Wallet } from "./types"
import { enablePayments, fetchWallet } from "./api"

const seen = (projectId: string) => `neuron.paymentsOffer.${projectId}`

// Shown once the first build exists and payments are not on yet: what they are and what they cost
export function PaymentsOffer({ projectId, onOpenWallet }: { projectId: string; onOpenWallet: () => void }) {
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [error, setError] = useState("")

  useEffect(() => {
    setWallet(null)
    if (!projectId || localStorage.getItem(seen(projectId))) return
    fetchWallet().then((w) => setWallet(w.offer ? w : null), () => setWallet(null)) // not the owner, or nothing built yet: no offer
  }, [projectId])

  if (!wallet) return null
  const dismiss = () => { localStorage.setItem(seen(projectId), "1"); setWallet(null) }
  const turnOn = () => enablePayments().then(() => { dismiss(); onOpenWallet() }, (e) => setError(e instanceof Error ? e.message : "Could not turn payments on."))
  return (
    <div className="profile-overlay" onClick={dismiss}>
      <div className="profile-modal waitlist-modal" role="dialog" aria-modal="true" aria-label="Introducing Payments" onClick={(e) => e.stopPropagation()}>
        <h2>Introducing Payments</h2>
        <p>Let customers pay on your site. Payments land in your Wallet, and you can pay them out {wallet.refund_days} days after each one, once the refund period has passed.</p>
        <p><strong>5% per payment</strong>: {wallet.platform_fee_pct}% to Neuron and about {wallet.gateway_fee_pct}% to the payment gateway, taken from each payment.</p>
        <p>Customers ask for refunds through your Wallet, and nothing is refunded until you approve it.</p>
        <p>Turning payments on has Muse add checkout to your site, in a coding session. Nothing goes public until you allow the publish card it leaves in your chat.</p>
        {error && <p role="alert">{error}</p>}
        <button type="button" className="btn btn-primary" onClick={turnOn} autoFocus>Turn on payments</button>
        <button type="button" className="btn" onClick={dismiss}>Not now</button>
      </div>
    </div>
  )
}
