"""Stripe test-mode money movement: deposit (Checkout or direct), transfer per clause."""
import os

import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
if stripe.api_key and not stripe.api_key.startswith("sk_test_"):
    raise RuntimeError("Test mode only: STRIPE_SECRET_KEY must start with sk_test_")


def create_checkout(deal: dict, base_url: str) -> str:
    session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{
            "quantity": 1,
            "price_data": {
                "currency": deal["currency"],
                "unit_amount": deal["total"],
                "product_data": {"name": f"Escrow deposit: {deal['title']}"},
            },
        }],
        payment_intent_data={
            "transfer_group": deal["deal_id"],
            "metadata": {"deal_id": deal["deal_id"], "role": "client_deposit"},
        },
        metadata={"deal_id": deal["deal_id"]},
        success_url=f"{base_url}/deal/funded?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}/",
    )
    return session.url


def confirm_checkout(session_id: str) -> dict | None:
    """Returns {payment_intent, charge, amount} if paid, else None."""
    s = stripe.checkout.Session.retrieve(session_id, expand=["payment_intent"])
    if s.payment_status != "paid":
        return None
    pi = s.payment_intent
    return {"payment_intent": pi.id, "charge": pi.latest_charge, "amount": pi.amount}


def direct_deposit(deal: dict) -> dict:
    """Demo shortcut: charge test card 4000 0000 0000 0077 (funds available immediately)."""
    pi = stripe.PaymentIntent.create(
        amount=deal["total"],
        currency=deal["currency"],
        payment_method="pm_card_bypassPending",
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        confirm=True,
        transfer_group=deal["deal_id"],
        description=f"Escrow deposit: {deal['title']}",
        metadata={"deal_id": deal["deal_id"], "role": "client_deposit"},
    )
    return {"payment_intent": pi.id, "charge": pi.latest_charge, "amount": pi.amount}


def transfer(deal: dict, c: dict, rule: str) -> str:
    tr = stripe.Transfer.create(
        amount=c["amount"],
        currency=deal["currency"],
        destination=deal["contractor_account"],
        transfer_group=deal["deal_id"],
        source_transaction=deal["deposit"]["charge"],
        description=f"{deal['deal_id']} clause {c['id']}: {c['deliverable']}",
        metadata={"deal_id": deal["deal_id"], "clause": c["id"], "rule": rule},
    )
    return tr.id
