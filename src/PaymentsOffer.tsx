import { useEffect, useRef, useState } from "react"
import type { Wallet } from "./types"
import { enablePayments, fetchWallet } from "./api"

const seen = (projectId: string) => `neuron.paymentsOffer.${projectId}`

// Shown once the first build exists and payments are not on yet: what they are and what they cost
export function PaymentsOffer({ projectId, onOpenWallet, hold = false }: { projectId: string; onOpenWallet: () => void; hold?: boolean }) {
  const [wallet, setWallet] = useState<Wallet | null>(null)
  const [error, setError] = useState("")
  const [at, setAt] = useState(0)
  const track = useRef<HTMLDivElement>(null)
  const go = (i: number) => track.current?.scrollTo({ left: i * track.current.clientWidth, behavior: "smooth" })

  // Asks whether the offer is due: the first build exists and payments are not on. `force` is for "Turn on payments" in the Wallet, which shows it again even if it was dismissed.
  useEffect(() => {
    setWallet(null)
    if (!projectId) return
    let live = true
    const check = (force: boolean) => {
      if (!force && localStorage.getItem(seen(projectId))) return
      fetchWallet().then((w) => { if (live && w.offer) { setAt(0); setWallet(w) } }, () => {})  // not the owner, or nothing built yet: no offer
    }
    check(false)
    // The build can finish while the page is open, so it is asked again now and then, and when the person comes back to the tab, until it has been seen
    const timer = window.setInterval(() => check(false), 20_000)
    const onFocus = () => check(false)
    const onAsk = () => check(true)
    window.addEventListener("focus", onFocus)
    window.addEventListener("neuron:payments-offer", onAsk)
    return () => { live = false; window.clearInterval(timer); window.removeEventListener("focus", onFocus); window.removeEventListener("neuron:payments-offer", onAsk) }
  }, [projectId])

  if (!wallet || hold) return null
  const dismiss = () => { localStorage.setItem(seen(projectId), "1"); setWallet(null) }
  const turnOn = () => enablePayments().then(() => { dismiss(); onOpenWallet() }, (e) => setError(e instanceof Error ? e.message : "Could not turn payments on."))
  const total = wallet.platform_fee_pct + wallet.gateway_fee_pct
  const faq = [
    { q: "What does it cost?", a: `${total}% per payment: ${wallet.platform_fee_pct}% to Neuron and about ${wallet.gateway_fee_pct}% to the payment gateway, taken from each payment.` },
    { q: "When do I get paid?", a: `${wallet.refund_days} days after each payment, once the refund period has passed. Payouts are requested from your Wallet.` },
    { q: "What about refunds?", a: "Customers ask for a refund through your Wallet, and nothing is refunded until you approve it." },
    { q: "How is it set up?", a: "Muse adds checkout to your site in a coding session. Nothing goes public until you allow the publish card it leaves in your chat." },
  ]
  return (
    <div className="profile-overlay" onClick={dismiss}>
      <div className="profile-modal pay-offer" role="dialog" aria-modal="true" aria-label="Introducing Payments" onClick={(e) => e.stopPropagation()}>
        <h2 className="pay-offer-head src-run-head is-running"><span className="pay-offer-kicker">New</span><span>Introducing Payments</span></h2>
        <p className="pay-offer-lead">Let customers pay on your site. The money lands in your Wallet.</p>
        <div className="pay-faq" role="region" aria-roledescription="carousel" aria-label="Payments, in short">
          <div className="pay-faq-track" ref={track} onScroll={() => { const t = track.current; if (t) setAt(Math.round(t.scrollLeft / t.clientWidth)) }}>
            {faq.map((card, i) => (
              <section key={card.q} className="pay-faq-card" role="group" aria-roledescription="slide" aria-label={`${i + 1} of ${faq.length}`}>
                <h3>{card.q}</h3>
                <p>{card.a}</p>
              </section>
            ))}
          </div>
          <div className="pay-faq-nav">
            <button type="button" className="dm-share-no" disabled={at === 0} onClick={() => go(at - 1)}>← Back</button>
            <span className="pay-faq-dots" aria-hidden="true">{faq.map((c, i) => <i key={c.q} className={i === at ? "is-on" : ""} />)}</span>
            <button type="button" className="dm-share-no" disabled={at === faq.length - 1} onClick={() => go(at + 1)}>Next →</button>
          </div>
        </div>
        {error && <p role="alert" className="src-approval-error">{error}</p>}
        <div className="dm-share-actions">
          <button type="button" className="dm-share-no" onClick={dismiss}>Not now</button>
          <button type="button" className="dm-share-yes" onClick={turnOn} autoFocus>Turn on payments</button>
        </div>
      </div>
    </div>
  )
}
