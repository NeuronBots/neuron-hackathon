"""Payments for the sites Neuron hosts: what the owner's wallet holds, refunds the owner approves, and payouts after the refund period.

Customers pay through Razorpay Checkout on Neuron's account. Neuron keeps PLATFORM_FEE_PCT of every payment; the gateway's own fee comes out too.
The wallet is worked out from the payments table, never stored, so it cannot drift from it. Test-mode payments are listed but never counted."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from core.models import Payment, Payout, PriceItem, Project, ProjectRecord

log = logging.getLogger(__name__)

PLATFORM_FEE_PCT = 3
GATEWAY_FEE_PCT = 2  # ponytail: an estimate for the "5% in all" the owner is shown; the webhook writes Razorpay's real fee over it
REFUND_DAYS = int(os.getenv("PAYMENT_REFUND_DAYS", "7"))


def has_build(db: Session, project_id: str) -> bool:
    """The project has a saved first build: there is a site to add checkout to."""
    from core.models import DocumentRecord

    return db.query(DocumentRecord).filter(DocumentRecord.project_id == project_id, DocumentRecord.filename.like("%-first-build-%.zip")).first() is not None


def whole(amount: int) -> float:
    """An amount in whole units (rupees, dollars) for what people and bots read: 50000 paise is 500.0, never 50000 INR. ponytail: two decimals; a currency with none (yen) would need its own rule."""
    return round(amount / 100, 2)


def platform_fee(amount: int) -> int:
    """3% of the amount, rounded to the nearest unit."""
    return (amount * PLATFORM_FEE_PCT + 50) // 100


def net(p: Payment) -> int:
    return p.amount - p.platform_fee - p.gateway_fee


def _bucket(p: Payment, now: datetime) -> str:
    if p.test:
        return "test"
    if p.status == "paid":
        return "available" if p.paid_at and now - p.paid_at >= timedelta(days=REFUND_DAYS) else "pending"
    return {"refund_requested": "pending", "refunded": "refunded", "paid_out": "paid_out"}.get(p.status, "none")


def wallet(db: Session, project_id: str, now: datetime | None = None) -> dict:
    """pending: paid but still inside the refund period (or asked to be refunded); available: past it, ready to pay out. Per currency."""
    now = now or datetime.utcnow()
    out: dict[str, dict[str, int]] = {}
    for p in db.query(Payment).filter(Payment.project_id == project_id):
        bucket = _bucket(p, now)
        if bucket in ("test", "none"):
            continue
        row = out.setdefault(p.currency, {"pending": 0, "available": 0, "paid_out": 0, "refunded": 0})
        row[bucket] += p.amount if bucket == "refunded" else net(p)
    return out


def decide_refund(db: Session, payment: Payment, approve: bool, refund) -> str:
    """The owner's answer to a refund request. Approving sends the refund through `refund(razorpay_payment_id, amount)` first: only when Razorpay accepts does the row change.
    Raises ValueError with what the owner should hear."""
    if payment.status != "refund_requested":
        raise ValueError("That payment has no refund request waiting.")
    if approve:
        if not payment.razorpay_payment_id:
            raise ValueError("That payment was not taken through Razorpay, so there is nothing to refund there.")
        refund(payment.razorpay_payment_id, payment.amount)
        payment.status = "refunded"
    else:
        payment.status = "paid"
    db.commit()
    return payment.status


def request_payout(db: Session, project_id: str, user_id: str, now: datetime | None = None) -> list[Payout]:
    """Everything available becomes payout requests (one per currency) and those payments are marked paid out. Raises ValueError when nothing is available."""
    now = now or datetime.utcnow()
    due = [p for p in db.query(Payment).filter(Payment.project_id == project_id, Payment.status == "paid") if _bucket(p, now) == "available"]
    if not due:
        raise ValueError(f"Nothing is available yet: a payment can be paid out {REFUND_DAYS} days after it is taken.")
    made = []
    for currency in sorted({p.currency for p in due}):
        mine = [p for p in due if p.currency == currency]
        payout = Payout(project_id=project_id, requested_by=user_id, amount=sum(net(p) for p in mine), currency=currency)
        db.add(payout)
        db.flush()
        for p in mine:
            p.status, p.payout_id = "paid_out", payout.id
        made.append(payout)
    db.commit()
    return made


# ── the customer's side ──────────────────────────────────────────────────

MAX_LINES, MAX_QTY, MIN_AMOUNT = 20, 20, 100  # items in one order; of one item; Razorpay's smallest charge (1.00)


class PayError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def propose_prices(db: Session, project: Project, raw: str | None) -> int:
    """What Muse found on the site, kept for the owner to look at. Nothing here is charged: only the price list the owner saves is. Returns how many were kept."""
    try:
        found = json.loads(raw or "[]")
    except ValueError:
        return 0
    kept, seen = [], set()
    for item in found if isinstance(found, list) else []:
        try:
            sku, amount = re.sub(r"[^a-z0-9_-]+", "-", str(item["sku"]).lower()).strip("-")[:40], round(float(item["price"]) * 100)
            currency = str(item.get("currency") or "INR").upper()
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if sku and sku not in seen and MIN_AMOUNT <= amount <= 1_000_000_00 and re.fullmatch(r"[A-Z]{3}", currency):
            seen.add(sku)
            kept.append({"sku": sku, "name": str(item.get("name") or sku)[:120], "amount": amount, "currency": currency})
    project.proposed_prices = json.dumps(kept[:100]) if kept else ""
    db.commit()
    return len(kept)


def prices(db: Session, project_id: str) -> list[PriceItem]:
    return db.query(PriceItem).filter(PriceItem.project_id == project_id).order_by(PriceItem.name, PriceItem.sku).all()


def create_order(db: Session, project: Project, body: dict, gateway, key_id: str, preview: bool, site_base: str) -> dict:
    """A customer pays for items of the owner's price list. The amount is worked out here from that list; whatever the page says about prices is ignored.
    The money is taken on a Razorpay-hosted payment page (a payment link): Razorpay's own script cannot run in Neuron's sandboxed pages, and the card is typed on Razorpay's page either way.
    `gateway(method, path, json)` makes the Razorpay call; `site_base` is the site's address, where Razorpay sends the customer back. Raises PayError with what the page can show."""
    if not project.payments_enabled:
        raise PayError("Payments are not turned on for this site.", 403)
    test = key_id.startswith("rzp_test_")
    if preview and not test:
        raise PayError("Payments in the preview need Razorpay test keys, so a real card is never charged here.", 409)
    email = str(body.get("email") or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 120:
        raise PayError("Enter a valid email address.")
    catalog = {i.sku: i for i in prices(db, project.id)}
    if not catalog:
        raise PayError("Online payment is not ready yet: the shop has not set its prices.", 409)
    wanted = body.get("items")
    if not isinstance(wanted, list) or not wanted or len(wanted) > MAX_LINES:
        raise PayError(f"Send between 1 and {MAX_LINES} items.")
    lines, total, currency = [], 0, ""
    for want in wanted:
        want = want if isinstance(want, dict) else {}
        item = catalog.get(str(want.get("sku") or ""))
        try:
            qty = int(want.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if item is None or not 1 <= qty <= MAX_QTY:
            raise PayError("One of the items is not sold here, or its quantity is not between 1 and %d." % MAX_QTY)
        if currency and item.currency != currency:
            raise PayError("One order is paid in one currency.")
        currency = item.currency
        total += item.amount * qty
        lines.append({"sku": item.sku, "name": item.name, "qty": qty, "price": whole(item.amount)})
    if total < MIN_AMOUNT:
        raise PayError("The total is below the smallest amount that can be charged.")
    details = {k: v for k, v in body.items() if k != "items"}
    try:
        record = ProjectRecord(project_id=project.id, collection="orders", status="test" if test else "unpaid",
                               data={**details, "items": lines, "total": whole(total), "currency": currency})
        if len(json.dumps(record.data)) > 20_000:
            raise PayError("That order is too large.")
        db.add(record)
        db.flush()
        payment = Payment(project_id=project.id, record_id=record.id, amount=total, currency=currency, platform_fee=platform_fee(total),
                          gateway_fee=total * GATEWAY_FEE_PCT // 100, test=test)
        db.add(payment)
        db.flush()
        what = ", ".join(f"{l['qty']} x {l['name'] or l['sku']}" for l in lines)
        link = gateway("POST", "/payment_links", {
            "amount": total, "currency": currency, "accept_partial": False, "reference_id": payment.id,
            "description": f"{project.name}: {what}"[:200], "customer": {"name": str(details.get("name") or "")[:100], "email": email},
            "notify": {"sms": False, "email": False}, "reminder_enable": False, "expire_by": int(time.time()) + 3600,
            "callback_url": f"{site_base}?paid={payment.id}", "callback_method": "get", "notes": {"payment": payment.id, "project": project.id},
        })
        payment.razorpay_order_id = link["id"]  # the payment link's id, from this column's earlier life as an order's
        db.commit()
    except Exception:
        db.rollback()  # nothing is kept of an order Razorpay did not take
        raise
    return {"payment_id": payment.id, "pay_url": link["short_url"], "amount": total, "currency": currency, "test": test, "name": project.name}


def apply_event(db: Session, event: dict) -> Payment | None:
    """A Razorpay webhook event, already signature-checked. Returns the payment that has just become paid (so the caller can start the team on it), else None.
    The amount and currency Razorpay reports must match what was asked for; an event for anything else, or one seen before, changes nothing."""
    payload = event.get("payload") or {}
    paid = (payload.get("payment") or {}).get("entity") or {}
    kind = event.get("event", "")
    link = (payload.get("payment_link") or {}).get("entity") or {}
    p = db.get(Payment, str(link.get("reference_id") or "-")) if kind.startswith("payment_link.") else None
    if kind == "payment_link.paid":
        if p is None or p.status != "created" or (link.get("id") and link["id"] != p.razorpay_order_id):
            return None
        if link.get("amount") != p.amount or link.get("currency") != p.currency:
            log.warning("payment %s: Razorpay reports %s %s, asked for %s %s", p.id, link.get("amount"), link.get("currency"), p.amount, p.currency)
            return None
        p.status, p.razorpay_payment_id, p.paid_at = "paid", str(paid.get("id", ""))[:40], datetime.utcnow()
        if paid.get("fee") is not None:
            p.gateway_fee = int(paid["fee"]) + int(paid.get("tax") or 0)  # what Razorpay really took, with its tax
        record = db.get(ProjectRecord, p.record_id) if p.record_id else None
        if record is not None and not p.test:
            record.status = "paid"
        db.commit()
        return p
    if kind == "payment_link.expired":
        if p is not None and p.status == "created":
            p.status = "failed"
            db.commit()
        return None
    if kind in ("refund.processed", "refund.created"):
        refund = (payload.get("refund") or {}).get("entity") or {}
        p = db.query(Payment).filter(Payment.razorpay_payment_id == refund.get("payment_id", "-")).first() if refund.get("payment_id") else None
        if p is not None and p.status in ("paid", "refund_requested"):
            p.status = "refunded"
            db.commit()
    return None


def announce(db: Session, payment: Payment) -> None:
    """Tell the owner, in their chat with the project's Master, that a payment has come in. Never raises: the order's routines must still run."""
    try:
        from core.models import Agent, ProjectMember
        from services import chat_rows

        owner = db.query(ProjectMember).filter(ProjectMember.project_id == payment.project_id, ProjectMember.role == "owner").first()
        master = db.query(Agent).filter(Agent.project_id == payment.project_id, Agent.is_master == "true").first()
        if owner is None or master is None:
            return
        record = db.get(ProjectRecord, payment.record_id) if payment.record_id else None
        data = record.data if record and isinstance(record.data, dict) else {}
        items = [f"{l.get('qty', 1)}× {l.get('name', '')}" for l in data.get("items", []) if isinstance(l, dict)][:4]
        who = next((str(data[k])[:60] for k in ("name", "customer", "email") if data.get(k)), "")
        paid_at = payment.paid_at or datetime.utcnow()
        chat_rows.write_row(master.id, owner.user_id, chat_rows.card(
            "payment", amount=payment.amount, currency=payment.currency, net=net(payment), fee=payment.platform_fee + payment.gateway_fee, test=payment.test,
            who=who, items=items, ref=payment.id[:8], payout=(paid_at + timedelta(days=REFUND_DAYS)).strftime("%d %b")))
    except Exception:
        log.exception("Could not tell the owner about payment %s", payment.id)


def order_message(db: Session, payment: Payment) -> str:
    """What the order routines are given when an order has been paid: the order as the customer made it."""
    record = db.get(ProjectRecord, payment.record_id)
    return json.dumps(record.data if record else {}, ensure_ascii=False)[:4000]


def request_refund(db: Session, payment_id: str, reason: str, now: datetime | None = None) -> Payment:
    """The customer asks for their money back, inside the refund period. It waits in the owner's wallet for their yes."""
    p = db.get(Payment, payment_id)
    if p is None or p.status != "paid":
        raise PayError("There is no paid order with that reference to refund.", 404)
    if (now or datetime.utcnow()) - (p.paid_at or p.created_at) > timedelta(days=REFUND_DAYS):
        raise PayError(f"Refunds can be asked for within {REFUND_DAYS} days of paying.", 409)
    p.status, p.refund_reason = "refund_requested", reason.strip()[:500]
    db.commit()
    return p


def door(db: Session, project: Project, name: str, method: str, body: bytes, gateway, key_id: str, preview: bool, site_base: str = "") -> tuple[int, dict]:
    """The page's way to the payments, by name: _catalog (what is for sale), _pay (start paying; also _pay/<id> to ask how it went), _refund. Returns (status, JSON)."""
    try:
        if name == "_catalog" and method == "GET":
            return 200, {"ok": True, "enabled": project.payments_enabled, "items": [{"sku": i.sku, "name": i.name, "amount": i.amount, "currency": i.currency} for i in prices(db, project.id)]}
        if name.startswith("_pay/") and method == "GET":
            p = db.get(Payment, name[5:])
            return (200, {"ok": True, "status": p.status}) if p and p.project_id == project.id else (404, {"ok": False, "error": "No such payment"})
        if name in ("_pay", "_refund") and method == "POST":
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                raise PayError("Send JSON.")
            if not isinstance(data, dict):
                raise PayError("Send a JSON object.")
            if name == "_pay":
                return 200, {"ok": True, **create_order(db, project, data, gateway, key_id, preview, site_base)}
            p = db.get(Payment, str(data.get("payment_id") or ""))
            if p is None or p.project_id != project.id:
                raise PayError("There is no paid order with that reference to refund.", 404)
            request_refund(db, p.id, str(data.get("reason") or ""))
            return 200, {"ok": True}
    except PayError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except Exception as exc:
        if exc.__class__.__name__ == "RazorpayError":
            log.warning("Razorpay refused an order: %s", exc)
            return 502, {"ok": False, "error": "The payment provider did not accept the order. Try again in a moment."}
        raise
    return 404, {"ok": False, "error": "Not found"}
