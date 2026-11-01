"""The owner's wallet: what the project's site has earned, refund requests to answer, and payouts to ask for."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.access import get_current_user, get_project
from core.database import get_db
from core.models import Payment, Payout, PriceItem, Project, ProjectMember, User
from services import payments

router = APIRouter()


def _owner(project: Project, user: User, db: Session) -> None:
    row = db.query(ProjectMember).filter(ProjectMember.project_id == project.id, ProjectMember.user_id == user.id).first()
    if row is None or row.role != "owner":
        raise HTTPException(status_code=403, detail="Only the project owner can use the wallet")


def _payment(payment_id: str, project: Project, db: Session) -> Payment:
    row = db.get(Payment, payment_id)
    if row is None or row.project_id != project.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    return row


def _read(p: Payment) -> dict:
    return {"id": p.id, "amount": p.amount, "currency": p.currency, "platform_fee": p.platform_fee, "gateway_fee": p.gateway_fee, "net": payments.net(p),
            "status": p.status, "refund_reason": p.refund_reason, "test": p.test, "record_id": p.record_id,
            "created_at": p.created_at.isoformat() + "Z", "paid_at": p.paid_at.isoformat() + "Z" if p.paid_at else None}


@router.get("/api/wallet")
def get_wallet(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owner(project, user, db)
    built = payments.has_build(db, project.id)
    return {
        "enabled": project.payments_enabled,
        "offer": built and not project.payments_enabled,  # the first build exists: payments can be offered
        "platform_fee_pct": payments.PLATFORM_FEE_PCT, "gateway_fee_pct": payments.GATEWAY_FEE_PCT, "refund_days": payments.REFUND_DAYS,
        "balance": payments.wallet(db, project.id),
        "proposed_prices": json.loads(project.proposed_prices or "[]"),
        "prices": [{"sku": i.sku, "name": i.name, "amount": i.amount, "currency": i.currency} for i in payments.prices(db, project.id)],
        "payments": [_read(p) for p in db.query(Payment).filter(Payment.project_id == project.id).order_by(Payment.created_at.desc()).limit(200)],
        "payouts": [{"id": o.id, "amount": o.amount, "currency": o.currency, "status": o.status, "created_at": o.created_at.isoformat() + "Z"}
                    for o in db.query(Payout).filter(Payout.project_id == project.id).order_by(Payout.created_at.desc()).limit(50)],
    }


def _add_checkout(project: Project, user: User, db: Session) -> None:
    """Muse adds checkout to the site, in the background. 409 when there is no build yet to add it to."""
    from routes.projects import _runner_bot
    from services import checkout, runs

    if not payments.has_build(db, project.id):
        raise HTTPException(status_code=409, detail="Build the first version of the site first.")
    runs.spawn(checkout.add_checkout(project.id, user.id, _runner_bot(project.id, db)))


@router.post("/api/wallet/enable")
async def enable(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Turns payments on and has Muse add checkout to the built site."""
    _owner(project, user, db)
    if not project.payments_enabled:
        _add_checkout(project, user, db)  # refused before anything changes when there is nothing to add it to
        project.payments_enabled = True
        db.commit()
    return {"enabled": True}


@router.post("/api/wallet/checkout", status_code=202)
async def add_checkout_again(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Muse adds checkout again (it did not finish, or the site changed)."""
    _owner(project, user, db)
    if not project.payments_enabled:
        raise HTTPException(status_code=409, detail="Turn payments on first.")
    _add_checkout(project, user, db)
    return {"adding": True}


class RefundAnswer(BaseModel):
    approve: bool


@router.post("/api/wallet/payments/{payment_id}/refund")
def answer_refund(payment_id: str, body: RefundAnswer, project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Approving sends the refund to Razorpay; declining puts the payment back as paid."""
    _owner(project, user, db)
    from routes.billing import RazorpayError, _call

    def refund(razorpay_payment_id: str, amount: int) -> None:
        _call("POST", f"/payments/{razorpay_payment_id}/refund", {"amount": amount, "notes": {"project": project.id}})

    row = _payment(payment_id, project, db)
    try:
        return {"status": payments.decide_refund(db, row, body.approve, refund)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RazorpayError as exc:
        raise HTTPException(status_code=502, detail=f"Razorpay did not refund it: {exc}")


@router.post("/api/wallet/payout", status_code=201)
def ask_for_payout(project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    _owner(project, user, db)
    try:
        made = payments.request_payout(db, project.id, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return [{"id": o.id, "amount": o.amount, "currency": o.currency} for o in made]


class Price(BaseModel):
    sku: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    name: str = Field(max_length=120)
    amount: int = Field(ge=payments.MIN_AMOUNT, le=1_000_000_00)  # smallest unit
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")


@router.put("/api/wallet/prices")
def set_prices(body: list[Price], project: Project = Depends(get_project), user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """The owner's price list, replaced as a whole. These are the only prices a customer is ever charged."""
    _owner(project, user, db)
    if len(body) > 100 or len({p.sku for p in body}) != len(body):
        raise HTTPException(status_code=422, detail="Up to 100 items, each with its own code")
    db.query(PriceItem).filter(PriceItem.project_id == project.id).delete()
    db.add_all(PriceItem(project_id=project.id, sku=p.sku, name=p.name.strip(), amount=p.amount, currency=p.currency) for p in body)
    project.proposed_prices = ""  # the owner has answered: what Muse proposed is no longer waiting
    db.commit()
    return {"saved": len(body)}
