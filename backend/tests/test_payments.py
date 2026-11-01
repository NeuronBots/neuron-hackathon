"""The owner's wallet: fees, the refund period, refunds the owner approves, and payouts."""
from datetime import datetime, timedelta

import pytest

from core.models import Payment, Project
from services import payments


def _pay(db, status="paid", days_ago=0, amount=100_000, test=False, rzp="pay_1"):
    p = Payment(project_id="p", amount=amount, platform_fee=payments.platform_fee(amount), gateway_fee=amount * payments.GATEWAY_FEE_PCT // 100,
                status=status, test=test, razorpay_payment_id=rzp, paid_at=datetime.utcnow() - timedelta(days=days_ago))
    db.add(p)
    db.commit()
    return p


def test_the_platform_fee_is_three_percent_rounded_to_the_nearest_unit():
    assert payments.platform_fee(100_000) == 3_000 and payments.platform_fee(50) == 2 and payments.platform_fee(10) == 0


def test_the_wallet_holds_a_payment_until_the_refund_period_has_passed_and_ignores_test_ones(db):
    db.add(Project(id="p", name="P"))
    db.commit()
    _pay(db, days_ago=1)                       # pending
    _pay(db, days_ago=payments.REFUND_DAYS + 1)  # available
    _pay(db, days_ago=30, test=True)           # a test payment never counts
    _pay(db, status="refunded", days_ago=30)
    w = payments.wallet(db, "p")["INR"]
    assert w["pending"] == 95_000 and w["available"] == 95_000 and w["refunded"] == 100_000 and w["paid_out"] == 0  # 100000 - 3% - 2%


def test_a_refund_changes_the_row_only_when_razorpay_accepts_it(db):
    db.add(Project(id="p", name="P"))
    db.commit()
    p = _pay(db, status="refund_requested")

    def refuses(*a):
        raise RuntimeError("Razorpay said no")

    with pytest.raises(RuntimeError):
        payments.decide_refund(db, p, True, refuses)
    assert p.status == "refund_requested"
    sent = []
    assert payments.decide_refund(db, p, True, lambda pid, amount: sent.append((pid, amount))) == "refunded" and sent == [("pay_1", 100_000)]
    with pytest.raises(ValueError):  # answered already
        payments.decide_refund(db, p, False, lambda *a: None)
    q = _pay(db, status="refund_requested", rzp="pay_2")
    assert payments.decide_refund(db, q, False, lambda *a: None) == "paid"  # declined: back to paid


def test_a_payout_takes_only_what_is_available_and_only_once(db):
    db.add(Project(id="p", name="P"))
    db.commit()
    _pay(db, days_ago=1)
    with pytest.raises(ValueError):
        payments.request_payout(db, "p", "u")  # still inside the refund period
    old = _pay(db, days_ago=payments.REFUND_DAYS + 1)
    made = payments.request_payout(db, "p", "u")
    assert [(o.amount, o.currency) for o in made] == [(95_000, "INR")] and old.status == "paid_out" and old.payout_id == made[0].id
    with pytest.raises(ValueError):
        payments.request_payout(db, "p", "u")


# ── the customer's side ──────────────────────────────────────────────────

import hashlib
import hmac
import json

from core.models import PriceItem, ProjectRecord

BASE = "https://neuron.test/sites/lotus-1/"


def _shop(db, enabled=True):
    p = Project(id="p", name="Lotus", payments_enabled=enabled, site_slug="lotus-1")
    db.add(p)
    db.flush()  # the project first: foreign keys are enforced
    db.add_all([PriceItem(project_id="p", sku="drop-in", name="Drop-in", amount=50_000), PriceItem(project_id="p", sku="pack", name="10-class pack", amount=400_000)])
    db.commit()
    return p


def _gateway(made):
    def call(method, path, body=None):
        made.append(body)
        return {"id": f"plink_{len(made)}", "short_url": f"https://rzp.io/{len(made)}"}
    return call


def _order(db, p, items, key="rzp_test_k", preview=False, made=None, **extra):
    return payments.create_order(db, p, {"items": items, "email": "a@x.io", **extra}, _gateway([] if made is None else made), key, preview, BASE)


def test_a_shop_without_prices_says_so_instead_of_refusing_every_item(db):
    p = Project(id="p", name="Lotus", payments_enabled=True, site_slug="l")
    db.add(p)
    db.commit()
    with pytest.raises(payments.PayError, match="not set its prices") as e:
        payments.create_order(db, p, {"items": [{"sku": "x"}], "email": "a@x.io"}, _gateway([]), "rzp_test_k", False, BASE)
    assert e.value.status == 409


def test_the_amount_is_the_owners_price_never_the_pages(db):
    p, made = _shop(db), []
    out = _order(db, p, [{"sku": "drop-in", "qty": 2}, {"sku": "pack"}], made=made, amount=1, total=1, name="Asha")
    assert out["amount"] == 500_000 and made[0]["amount"] == 500_000 and out["test"] is True  # 2 x 500 + 4000 rupees, in paise
    pay = db.query(Payment).one()
    assert pay.platform_fee == 15_000 and pay.status == "created" and pay.razorpay_order_id == "plink_1" and out["pay_url"] == "https://rzp.io/1"
    assert made[0]["reference_id"] == pay.id and made[0]["callback_url"] == f"{BASE}?paid={pay.id}"  # Razorpay sends the customer back to the site
    rec = db.get(ProjectRecord, pay.record_id)
    assert rec.status == "test" and rec.data["email"] == "a@x.io" and rec.data["total"] == 5000.0 and rec.data["items"][0]["price"] == 500.0  # whole units, never "50000 INR" for a 500 rupee class; the page's own "total" is overwritten


def test_an_order_is_refused_for_what_is_not_sold_not_enabled_no_email_or_in_the_preview_on_a_live_key(db):
    p = _shop(db, enabled=False)
    with pytest.raises(payments.PayError) as e:
        _order(db, p, [{"sku": "pack"}])
    assert e.value.status == 403
    p.payments_enabled = True
    for items in ([], [{"sku": "nope"}], [{"sku": "pack", "qty": 0}], [{"sku": "pack", "qty": 99}], "pack"):
        with pytest.raises(payments.PayError):
            _order(db, p, items)
    for email in ("", "not an email", "+91 98765 43210"):  # contact is an email: a phone number is not taken
        with pytest.raises(payments.PayError, match="email"):
            payments.create_order(db, p, {"items": [{"sku": "pack"}], "email": email}, _gateway([]), "rzp_test_k", False, BASE)
    with pytest.raises(payments.PayError) as e:  # a real key never charges from the preview
        _order(db, p, [{"sku": "pack"}], key="rzp_live_k", preview=True)
    assert e.value.status == 409 and db.query(Payment).count() == 0


def test_razorpay_failing_leaves_no_half_made_order(db):
    p = _shop(db)

    def broken(*a):
        raise RuntimeError("down")

    with pytest.raises(RuntimeError):
        payments.create_order(db, p, {"items": [{"sku": "pack"}], "email": "a@x.io"}, broken, "rzp_test_k", False, BASE)
    assert db.query(Payment).count() == 0 and db.query(ProjectRecord).count() == 0


def _link_paid(pay, amount=None, fee=None, link_id=None):
    entity = {"id": "pay_9", "order_id": "order_x", "amount": pay.amount, "currency": pay.currency}
    if fee is not None:
        entity.update(fee=fee, tax=fee // 5)
    link = {"id": link_id or pay.razorpay_order_id, "reference_id": pay.id, "amount": pay.amount if amount is None else amount, "currency": pay.currency, "status": "paid"}
    return {"event": "payment_link.paid", "payload": {"payment_link": {"entity": link}, "payment": {"entity": entity}}}


def test_a_payment_is_paid_only_when_razorpay_says_so_with_the_right_amount_and_only_once(db):
    p = _shop(db)
    _order(db, p, [{"sku": "pack"}], key="rzp_live_k")
    pay = db.query(Payment).one()
    assert payments.apply_event(db, _link_paid(pay, amount=1)) is None and pay.status == "created"  # the wrong amount
    assert payments.apply_event(db, _link_paid(pay, link_id="plink_other")) is None  # another link claiming this reference
    done = payments.apply_event(db, _link_paid(pay, fee=8_000))
    assert done is pay and pay.status == "paid" and pay.razorpay_payment_id == "pay_9" and pay.gateway_fee == 9_600 and pay.paid_at
    assert db.get(ProjectRecord, pay.record_id).status == "paid"
    assert payments.apply_event(db, _link_paid(pay)) is None  # seen before: nothing starts twice
    stale = payments.apply_event(db, {"event": "payment_link.expired", "payload": {"payment_link": {"entity": {"reference_id": pay.id}}}})
    assert stale is None and pay.status == "paid"  # an expiry never undoes a payment


def test_a_link_that_expires_unpaid_is_marked_failed(db):
    p = _shop(db)
    _order(db, p, [{"sku": "pack"}])
    pay = db.query(Payment).one()
    payments.apply_event(db, {"event": "payment_link.expired", "payload": {"payment_link": {"entity": {"reference_id": pay.id}}}})
    assert pay.status == "failed"


def test_a_customer_can_ask_for_a_refund_inside_the_refund_period_only(db):
    db.add(Project(id="p", name="P"))
    db.commit()
    fresh, old = _pay(db, days_ago=1, rzp="pay_a"), _pay(db, days_ago=payments.REFUND_DAYS + 1, rzp="pay_b")
    assert payments.request_refund(db, fresh.id, "changed my mind").status == "refund_requested" and fresh.refund_reason == "changed my mind"
    with pytest.raises(payments.PayError) as e:
        payments.request_refund(db, old.id, "late")
    assert e.value.status == 409
    with pytest.raises(payments.PayError):
        payments.request_refund(db, fresh.id, "twice")  # no longer paid


def test_the_webhook_route_checks_the_signature_and_the_door_answers_the_page(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.database import get_db
    from routes import billing

    p = _shop(db)
    _order(db, p, [{"sku": "drop-in"}])
    pay = db.query(Payment).one()
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "s3cret")
    app = FastAPI()
    app.include_router(billing.router)
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    raw = json.dumps(_link_paid(pay)).encode()
    assert client.post("/api/billing/webhook", content=raw, headers={"x-razorpay-signature": "bad"}).status_code == 400
    assert db.query(Payment).one().status == "created"
    sign = hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
    monkeypatch.setattr("services.sites.project_of", lambda pid: None)  # the team's start is tested with the sites
    assert client.post("/api/billing/webhook", content=raw, headers={"x-razorpay-signature": sign}).status_code == 200
    assert db.query(Payment).one().status == "paid"
    status, out = payments.door(db, p, "_catalog", "GET", b"", None, "", False)
    assert status == 200 and [i["sku"] for i in out["items"]] == ["pack", "drop-in"]
    assert payments.door(db, p, f"_pay/{pay.id}", "GET", b"", None, "", False) == (200, {"ok": True, "status": "paid"})
    assert payments.door(db, p, "_refund", "POST", json.dumps({"payment_id": pay.id, "reason": "x"}).encode(), None, "", False)[0] == 200
    assert payments.door(db, p, "_pay", "POST", b"not json", None, "rzp_test_k", False)[0] == 422


def test_the_checkout_request_tells_muse_to_call_the_server_and_hold_no_key():
    from services import build_prompt as bp
    text = bp.payments_request("Lotus")
    for must in ("GET _catalog", "POST _pay", "pay_url", "?paid=", "_pay/<payment_id>", "POST _refund", "Never send an amount", "not proof of payment", "Never put a key or a secret", "do not ask for a phone number"):
        assert must in text
    assert "rzp_" not in text and "key_secret" not in text and "Do NOT load Razorpay's checkout.js" in text  # the page never loads Razorpay's script


def test_turning_payments_on_needs_a_build_and_leaves_the_flag_alone_when_refused(db, monkeypatch):
    from fastapi import HTTPException

    from core.models import Agent, ProjectMember, User
    from routes import wallet as rw
    from services import checkout, runs

    user = User(id="u", name="U", email="u@x.io", password_hash="x")
    project = Project(id="p", name="P")
    db.add_all([user, project])
    db.flush()
    db.add_all([Agent(id="m", name="Master", system_prompt="x", project_id="p", is_master="true"), ProjectMember(project_id="p", user_id="u", role="owner")])
    db.commit()
    import asyncio

    with pytest.raises(HTTPException) as e:
        asyncio.run(rw.enable(project, user, db))
    assert e.value.status_code == 409 and project.payments_enabled is False
    started = []

    async def fake(project_id, user_id, agent_id):
        started.append((project_id, user_id, agent_id))

    monkeypatch.setattr(checkout, "add_checkout", fake)
    def spawn(coro):  # as the real one does, it needs the event loop: it fails when the endpoint runs in a worker thread
        asyncio.get_running_loop()
        coro.close()
        started.append("spawned")

    monkeypatch.setattr(runs, "spawn", spawn)
    monkeypatch.setattr(payments, "has_build", lambda db, pid: True)
    assert asyncio.run(rw.enable(project, user, db)) == {"enabled": True} and project.payments_enabled and started == ["spawned"]
    started.clear()
    asyncio.run(rw.enable(project, user, db))
    assert started == []  # on already: nothing is started twice
    assert asyncio.run(rw.add_checkout_again(project, user, db)) == {"adding": True} and started == ["spawned"]  # and again on request


def test_prices_muse_found_are_kept_as_a_proposal_cleaned_and_never_charged(db):
    p = _shop(db)
    raw = json.dumps([{"sku": "Drop In!", "name": "Drop-in", "price": 500}, {"sku": "pack", "price": "4000.50", "currency": "inr"},
                      {"sku": "drop-in", "price": 1}, {"sku": "free", "price": 0}, {"sku": "bad", "price": "x"}, "junk", {"price": 5}])
    assert payments.propose_prices(db, p, raw) == 2  # the duplicate code, the free item, the unreadable ones and the nameless are dropped
    kept = json.loads(p.proposed_prices)
    assert kept == [{"sku": "drop-in", "name": "Drop-in", "amount": 50_000, "currency": "INR"}, {"sku": "pack", "name": "pack", "amount": 400_050, "currency": "INR"}]
    assert [(i.sku, i.amount) for i in payments.prices(db, "p")] == [("pack", 400_000), ("drop-in", 50_000)]  # the price list customers pay from is untouched
    assert payments.propose_prices(db, p, "not json") == 0 and json.loads(p.proposed_prices) == kept  # an unreadable file leaves the earlier proposal alone
    assert payments.propose_prices(db, p, "[]") == 0 and p.proposed_prices == ""  # an empty one clears it


def test_the_checkout_request_asks_for_the_prices_file_and_says_it_is_never_used_to_charge():
    from services import build_prompt as bp
    text = bp.payments_request("Lotus")
    assert "neuron-prices.json" in text and "never used to charge anyone" in text
    for one_checkout in ("There must be ONE checkout", "Do NOT add a second form", "merge it into this one flow", "Reuse the page's own look", "Need a refund?"):
        assert one_checkout in text  # payment goes into the checkout the page has: no parallel "Pay online" section


def test_bots_notes_are_kept_under_their_names_and_the_customers_fields_survive(db):
    from tools import records

    db.add(Project(id="p", name="P"))
    db.commit()
    out = records.run("save_record", {"collection": "orders", "data": {"name": "Asha", "notes": "window mat please"}, "status": "test"}, "p", "Customer", db)
    rid = out.split("Added: ")[1].split(" | ")[0]
    for bot in ("Daya", "Karuna", "Shanti"):
        assert records.run("save_record", {"collection": "orders", "id": rid, "note": f"{bot} checked it"}, "p", bot, db).startswith("Saved")
    data = db.get(ProjectRecord, rid).data
    assert data["bot_notes"] == {"Daya": "Daya checked it", "Karuna": "Karuna checked it", "Shanti": "Shanti checked it"}  # nobody's note replaced another's
    assert data["notes"] == "window mat please" and data["name"] == "Asha"  # and the customer's own fields are as they were
    assert records.run("save_record", {"collection": "orders", "id": rid, "note": "Daya again"}, "p", "Daya", db).startswith("Saved")
    assert db.get(ProjectRecord, rid).data["bot_notes"]["Daya"] == "Daya again" and len(db.get(ProjectRecord, rid).data["bot_notes"]) == 3


def test_the_plan_brief_says_payments_are_neurons_so_no_provider_gets_planned():
    from services import first_plan
    text = first_plan.brief("Shop", "a shop", "", "", [("Amber", "Ops", "does ops")], "Tanay")
    assert "Payments are Neuron's" in text and "Do not plan or research a payment provider" in text


def test_a_paid_order_tells_the_owner_in_the_masters_chat(db):
    import json as _json
    from core.models import Agent, Message, Payment, Project, ProjectMember, ProjectRecord, User
    from services import payments, chat_rows

    db.add_all([User(id="u", name="U", email="u@x", password_hash=""), Project(id="p", name="Shop")])
    db.commit()
    db.add_all([Agent(id="m", name="Master", system_prompt="", project_id="p", is_master="true"), ProjectMember(project_id="p", user_id="u", role="owner")])
    db.add(ProjectRecord(id="r1", project_id="p", collection="orders", status="paid", data={"name": "Asha", "items": [{"name": "Candle", "qty": 2}], "total": 500}))
    pay = Payment(id="abcdef123456", project_id="p", record_id="r1", amount=50000, platform_fee=1500, gateway_fee=1000, status="paid", paid_at=datetime(2026, 11, 1))
    db.add(pay)
    db.commit()
    payments.announce(db, pay)
    card = _json.loads(db.query(Message).filter(Message.agent_id == "m").one().content[len("[[SOURCES:"):-2])
    assert (card["kind"], card["amount"], card["net"], card["fee"], card["who"], card["items"], card["ref"], card["payout"]) == ("payment", 50000, 47500, 2500, "Asha", ["2× Candle"], "abcdef12", "08 Nov")


def test_the_offer_opens_once_the_first_build_is_saved_and_only_until_payments_are_on(db):
    from core.models import DocumentRecord, Project, ProjectMember, User
    from routes import wallet as wallet_route

    db.add_all([User(id="u", name="U", email="u@x", password_hash=""), Project(id="p", name="Shop")])
    db.commit()
    from core.models import Agent

    db.add_all([ProjectMember(project_id="p", user_id="u", role="owner"), Agent(id="m", name="Master", system_prompt="", project_id="p", is_master="true")])
    db.commit()
    project, user = db.get(Project, "p"), db.get(User, "u")
    assert wallet_route.get_wallet(project, user, db)["offer"] is False  # nothing built yet
    db.add(DocumentRecord(filename="shop-first-build-20261101-100000.zip", title="x", project_id="p", agent_id="m"))
    db.commit()
    assert wallet_route.get_wallet(project, user, db)["offer"] is True
    project.payments_enabled = True
    db.commit()
    assert wallet_route.get_wallet(project, user, db)["offer"] is False  # already on
