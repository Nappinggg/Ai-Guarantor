"""De-risk Stripe: deposit -> hold on platform -> transfer one clause to the contractor.

Run:  .venv/Scripts/python.exe scripts/test_stripe.py
Needs STRIPE_SECRET_KEY (sk_test_...) in .env. Creates a test connected account
if STRIPE_CONNECTED_ACCOUNT is empty and prints the id to paste into .env.
"""
import os
import sys
import time

import stripe
from dotenv import load_dotenv

load_dotenv()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
if not stripe.api_key.startswith("sk_test_"):
    sys.exit("STRIPE_SECRET_KEY must be a sk_test_ key (test mode only).")

client = stripe.StripeClient(stripe.api_key)
DEAL_ID = f"deal_test_{int(time.time())}"


def platform_info():
    acct = stripe.Account.retrieve()
    return acct.country, acct.default_currency


def create_contractor_account(country: str, currency: str) -> str:
    """Accounts v2 recipient (can receive transfers). Stripe collects KYC and covers losses."""
    acct = client.v2.core.accounts.create({
        "display_name": "Bob Dev",
        "contact_email": f"bob.dev+{int(time.time())}@example.com",
        "dashboard": "full",
        "identity": {"country": country.lower(), "entity_type": "individual"},
        "configuration": {
            "merchant": {"capabilities": {"card_payments": {"requested": True}}},
            "recipient": {"capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}}
        },
        "defaults": {
            "currency": currency,
            "responsibilities": {"fees_collector": "stripe", "losses_collector": "stripe"},
        },
        "metadata": {"role": "contractor"},
    })
    save_env("STRIPE_CONNECTED_ACCOUNT", acct.id)
    print(f"Created connected account {acct.id} (saved to .env)")
    return acct.id


def transfers_status(account_id: str) -> str:
    acct = client.v2.core.accounts.retrieve(account_id, {"include": ["configuration.recipient"]})
    if acct.configuration and acct.configuration.recipient:
        return acct.configuration.recipient.capabilities.stripe_balance.stripe_transfers.status
    # Accounts created in the Dashboard (v1): read the classic capability
    return stripe.Account.retrieve(account_id).to_dict()["capabilities"].get("transfers")


def onboarding_link(account_id: str) -> str:
    return client.v2.core.account_links.create({
        "account": account_id,
        "use_case": {
            "type": "account_onboarding",
            "account_onboarding": {
                "refresh_url": "https://example.com/reauth",
                "return_url": "https://example.com/done",
            },
        },
    }).url


def save_env(key: str, value: str):
    lines = open(".env", encoding="utf-8").read().splitlines()
    lines = [f"{key}={value}" if l.startswith(f"{key}=") else l for l in lines]
    open(".env", "w", encoding="utf-8").write("\n".join(lines) + "\n")


def wait_for_transfers(account_id: str, tries: int = 10) -> bool:
    for _ in range(tries):
        status = transfers_status(account_id)
        print(f"  transfers capability: {status}")
        if status == "active":
            return True
        time.sleep(2)
    return False


def main():
    country, currency = platform_info()
    print(f"Platform country={country} currency={currency}")

    account_id = os.getenv("STRIPE_CONNECTED_ACCOUNT") or create_contractor_account(country, currency)
    if not wait_for_transfers(account_id, tries=3):
        print(f"\nAccount {account_id} can't receive transfers yet. Finish onboarding "
              f"(test data, use the 'test' shortcuts) here, then re-run:\n  {onboarding_link(account_id)}")
        sys.exit(1)

    # 1. Client deposit (full deal amount). pm_card_bypassPending = card 4000 0000 0000 0077:
    #    funds land in AVAILABLE balance immediately, so transfers don't fail.
    pi = stripe.PaymentIntent.create(
        amount=50000,
        currency=currency,
        payment_method="pm_card_bypassPending",
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
        confirm=True,
        transfer_group=DEAL_ID,
        description=f"Escrow deposit for {DEAL_ID}",
        metadata={"deal_id": DEAL_ID, "role": "client_deposit"},
    )
    print(f"Deposit {pi.id} status={pi.status} charge={pi.latest_charge}")

    # 2. Release clause C1 (half) to the contractor, tied to the deposit charge.
    tr = stripe.Transfer.create(
        amount=25000,
        currency=currency,
        destination=account_id,
        transfer_group=DEAL_ID,
        source_transaction=pi.latest_charge,
        description=f"{DEAL_ID} clause C1 release",
        metadata={"deal_id": DEAL_ID, "clause": "C1", "rule": "P3"},
    )
    print(f"Transfer {tr.id} amount={tr.amount} -> {tr.destination}")

    print("\nSUCCESS. Add to .env if not there yet:")
    print(f"  STRIPE_CONNECTED_ACCOUNT={account_id}")
    print("Check: https://dashboard.stripe.com/test/connect/transfers")


if __name__ == "__main__":
    main()
