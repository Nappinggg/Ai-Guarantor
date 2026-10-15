"""Deterministic policy engine: the ONLY gate to a payout. The LLM never decides whether to pay."""
import os
from datetime import datetime

from . import store


def check(rule: str, ok: bool, reason: str) -> dict:
    return {"rule": rule, "ok": ok, "reason": reason}


def release_checks(deal: dict, c: dict, via: str) -> list[dict]:
    """via: 'client_accept' or 'auto_release'. All checks must pass to release clause c."""
    paid = sum(x["amount"] for x in deal["clauses"] if x["status"] == "paid")
    deposit = deal["deposit"]["amount"] if deal["deposit"].get("paid") else 0
    expected_acct = os.getenv("STRIPE_CONNECTED_ACCOUNT", "")
    checks = [
        check("P5", deal["status"] in ("funded", "completed") and set(deal["approved_by"]) == {"client", "contractor"},
              "Contract confirmed by both parties and funded" if deal["status"] in ("funded", "completed")
              else "Contract not confirmed by both parties and funded yet"),
        check("P1", bool(deal["contractor_account"]) and deal["contractor_account"] == expected_acct,
              f"Payee is the contracted account {deal['contractor_account'] or '(none)'}"),
        check("P2", paid + c["amount"] <= deposit,
              f"Paid so far £{paid/100:.2f} + £{c['amount']/100:.2f} vs deposit £{deposit/100:.2f}"),
        check("P4", c["status"] != "frozen", "Clause is not under dispute" if c["status"] != "frozen"
              else "Clause is frozen by a dispute"),
        check("P3", c["status"] == "verified",
              f"Evidence verified ({c['evidence']['type']})" if c["status"] == "verified"
              else f"Evidence not verified (clause status: {c['status']})"),
    ]
    if via == "client_accept":
        checks.append(check("P3", True, "Client clicked Accept"))
    else:
        due = auto_release_due(deal, c)
        checks.append(check("P3", due, f"Auto-release window of {c['auto_release_days']} days "
                                        f"{'passed with no objection' if due else 'has not passed'}"))
    return checks


def auto_release_due(deal: dict, c: dict) -> bool:
    if c["status"] != "verified" or not c.get("verified_at") or not c.get("auto_release_days"):
        return False
    verified_at = datetime.strptime(c["verified_at"], "%Y-%m-%d %H:%M:%S")
    return (store.now(deal) - verified_at).days >= c["auto_release_days"]


def passed(checks: list[dict]) -> bool:
    return all(x["ok"] for x in checks)


def first_failure(checks: list[dict]) -> dict | None:
    return next((x for x in checks if not x["ok"]), None)
