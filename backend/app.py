"""AI Guarantor API. Run: .venv/Scripts/python.exe -m uvicorn backend.app:app --reload --port 8000"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

import os  # noqa: E402

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, RedirectResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import github_svc, llm, rules, store, stripe_svc  # noqa: E402
from .policy import POLICY, policy_text  # noqa: E402

app = FastAPI(title="AI Guarantor")
STATIC = Path(__file__).resolve().parent.parent / "static"
ROLES = {"client", "contractor"}


class Terms(BaseModel):
    terms: str


class Role(BaseModel):
    role: str


class Fund(BaseModel):
    mode: str = "checkout"  # "checkout" (Stripe-hosted page) or "direct" (test card, instant)


class Evidence(BaseModel):
    role: str = "contractor"
    pr_url: str = ""
    note: str = ""


class Chat(BaseModel):
    role: str
    message: str


class FastForward(BaseModel):
    days: int = 7


def _deal() -> dict:
    deal = store.load()
    if not deal:
        raise HTTPException(404, "No deal yet. POST /deal/draft first.")
    return deal


def _role(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(400, f"role must be one of {sorted(ROLES)}")
    return role


def _gbp(pence: int) -> str:
    return f"£{pence / 100:.2f}"


# ---------------- pages ----------------

@app.get("/")
def index():
    page = STATIC / "index.html"
    return FileResponse(page) if page.exists() else {"msg": "UI not built yet; see /docs"}


@app.get("/policy")
def get_policy():
    return POLICY


@app.get("/config")
def config():
    """Platform account id so dashboard links open the right sandbox."""
    try:
        return {"stripe_account": stripe_svc.stripe.Account.retrieve().id}
    except Exception:  # noqa: BLE001
        return {"stripe_account": None}


@app.get("/deal")
def get_deal():
    deal = store.load()
    if deal:
        deal["paid_total"] = sum(c["amount"] for c in deal["clauses"] if c["status"] == "paid")
        deal["now"] = store.ts(deal)
    return deal or {}


@app.post("/deal/reset")
def reset():
    store.reset()
    return {"ok": True}


# ---------------- 1. contract ----------------

@app.post("/deal/draft")
def draft(body: Terms):
    existing = store.load()
    if existing and existing["status"] != "draft":
        raise HTTPException(409, "P5: contract is already confirmed by both parties; it can't be redrafted.")
    data = llm.draft_contract(body.terms)
    clauses = [{
        "id": f"C{i + 1}",
        "deliverable": c["deliverable"],
        "amount": round(float(c["amount"]) * 100),
        "evidence": {"type": c.get("evidence_type", "github_pr"),
                     "description": c.get("evidence_description", "Pull request merged on GitHub")},
        "auto_release_days": int(c.get("auto_release_days") or 7),
        "status": "pending",
        "evidence_found": None,
        "verified_at": None,
        "transfer_id": None,
        "resolve_votes": [],
    } for i, c in enumerate(data["clauses"])]
    deal = {
        "deal_id": store.new_deal_id(),
        "title": data.get("title", "Deal"),
        "client": data.get("client", "Client"),
        "contractor": data.get("contractor", "Contractor"),
        "contractor_account": os.getenv("STRIPE_CONNECTED_ACCOUNT", ""),
        "currency": data.get("currency", "gbp").lower(),
        "total": sum(c["amount"] for c in clauses),
        "terms": body.terms,
        "approved_by": [],
        "status": "draft",
        "deposit": {"paid": False, "amount": 0},
        "clock_offset_days": 0,
        "clauses": clauses,
        "events": [],
    }
    store.add_event(deal, "agent", "CONTRACT_DRAFTED",
                    f"Drafted {len(clauses)} clauses totalling {_gbp(deal['total'])} from the parties' terms: "
                    f"\"{body.terms}\"")
    store.save(deal)
    return deal


@app.post("/deal/approve")
def approve(body: Role):
    deal = _deal()
    role = _role(body.role)
    if deal["status"] != "draft":
        raise HTTPException(409, "Contract already confirmed.")
    if role not in deal["approved_by"]:
        deal["approved_by"].append(role)
        store.add_event(deal, role, "APPROVED", f"{role.title()} approved the contract.")
    if set(deal["approved_by"]) == ROLES:
        deal["status"] = "approved"
        store.add_event(deal, "agent", "CONTRACT_LOCKED",
                        "Both parties confirmed. The contract is now the agent's rules and can only change "
                        "with both parties' consent.", rule="P5")
    store.save(deal)
    return deal


# ---------------- 2. funding ----------------

@app.post("/deal/fund")
def fund(body: Fund, request: Request):
    deal = _deal()
    if deal["status"] != "approved":
        raise HTTPException(409, f"Deal must be approved by both parties first (status: {deal['status']}).")
    if body.mode == "direct":
        dep = stripe_svc.direct_deposit(deal)
        return _record_deposit(deal, dep)
    url = stripe_svc.create_checkout(deal, str(request.base_url).rstrip("/"))
    return {"checkout_url": url}


@app.get("/deal/funded")
def funded(session_id: str):
    deal = _deal()
    dep = stripe_svc.confirm_checkout(session_id)
    if dep and not deal["deposit"].get("paid"):
        _record_deposit(deal, dep)
    return RedirectResponse("/")


def _record_deposit(deal: dict, dep: dict) -> dict:
    deal["deposit"] = {"paid": True, **dep}
    deal["status"] = "funded"
    store.add_event(deal, "client", "FUNDED",
                    f"Client deposited {_gbp(dep['amount'])}. Funds are held on the platform until a clause is "
                    f"released.", stripe_id=dep["payment_intent"])
    store.save(deal)
    return deal


# ---------------- 3. evidence ----------------

@app.post("/clause/{cid}/evidence")
def submit_evidence(cid: str, body: Evidence):
    deal = _deal()
    c = store.clause(deal, cid)
    if deal["status"] not in ("funded", "completed"):
        raise HTTPException(409, "Deal is not funded yet.")
    if c["status"] in ("paid", "frozen"):
        raise HTTPException(409, f"Clause {cid} is {c['status']}.")
    store.add_event(deal, body.role, "EVIDENCE_SUBMITTED", f"Submitted evidence for {cid}: {body.pr_url or body.note}",
                    clause=cid)

    if c["evidence"]["type"] == "github_pr":
        pr = github_svc.check_pr(body.pr_url)
        if not pr["ok"]:
            store.add_event(deal, "agent", "EVIDENCE_REJECTED", pr["error"], clause=cid, rule="P3")
            store.save(deal)
            return deal
        found = {k: pr[k] for k in ("url", "number", "title", "merged", "merged_at", "state", "ci",
                                    "changed_files", "additions", "deletions")}
        if not pr["merged"] or pr["ci"] == "failure":
            why = "PR is not merged" if not pr["merged"] else "CI checks failed"
            c["evidence_found"] = found
            store.add_event(deal, "agent", "EVIDENCE_REJECTED",
                            f"{why} (PR #{pr['number']}, state={pr['state']}, CI={pr['ci']}). "
                            f"Agreed evidence: {c['evidence']['description']}.", clause=cid, rule="P3", evidence=found)
            store.save(deal)
            return deal
        found["summary"] = llm.summarize_evidence(c, pr)
    else:
        found = {"note": body.note or body.pr_url, "summary": f"Contractor reports: {body.note or body.pr_url}. "
                                                              f"Client must review."}

    c["evidence_found"] = found
    c["status"] = "verified"
    c["verified_at"] = store.ts(deal)
    store.add_event(deal, "agent", "EVIDENCE_VERIFIED",
                    f"Agreed evidence for {cid} is present. Waiting for the client to Accept or Dispute "
                    f"(auto-release after {c['auto_release_days']} days without objection).",
                    clause=cid, rule="P3", evidence=found)
    store.save(deal)
    return deal


# ---------------- 4. release / dispute ----------------

def _release(deal: dict, c: dict, via: str, actor: str) -> dict:
    checks = rules.release_checks(deal, c, via)
    if not rules.passed(checks):
        fail = rules.first_failure(checks)
        store.add_event(deal, "agent", "RELEASE_REFUSED", f"Refused to release {c['id']}: {fail['reason']}.",
                        clause=c["id"], rule=fail["rule"], evidence={"checks": checks})
        store.save(deal)
        return {"released": False, "checks": checks, "deal": deal}
    try:
        tr_id = stripe_svc.transfer(deal, c, rule="P3")
    except Exception as e:  # noqa: BLE001 - surface Stripe errors in the log
        store.add_event(deal, "agent", "PAYOUT_FAILED", f"Stripe transfer failed: {str(e)[:300]}", clause=c["id"])
        store.save(deal)
        return {"released": False, "error": str(e), "deal": deal}
    c["status"] = "paid"
    c["transfer_id"] = tr_id
    trigger = "the client clicked Accept" if via == "client_accept" else \
        f"{c['auto_release_days']} days passed since verification with no objection"
    store.add_event(deal, "agent", "PAYOUT",
                    f"Paid {_gbp(c['amount'])} to {deal['contractor']} for {c['id']} ({c['deliverable']}): "
                    f"evidence verified and {trigger}. All checks P1-P5 passed.",
                    clause=c["id"], rule="P3", stripe_id=tr_id,
                    evidence={"found": c["evidence_found"], "checks": checks, "trigger": actor})
    if all(x["status"] == "paid" for x in deal["clauses"]):
        deal["status"] = "completed"
        store.add_event(deal, "agent", "DEAL_COMPLETED", "All clauses paid. Deal complete.")
    store.save(deal)
    return {"released": True, "checks": checks, "deal": deal}


@app.post("/clause/{cid}/accept")
def accept(cid: str, body: Role):
    deal = _deal()
    if _role(body.role) != "client":
        raise HTTPException(403, "Only the client can accept a delivery.")
    c = store.clause(deal, cid)
    store.add_event(deal, "client", "ACCEPTED", f"Client accepted the delivery for {cid}.", clause=cid)
    return _release(deal, c, "client_accept", "client")


@app.post("/clause/{cid}/dispute")
def dispute(cid: str, body: Role):
    deal = _deal()
    return _freeze(deal, store.clause(deal, cid), _role(body.role))


def _freeze(deal: dict, c: dict, role: str) -> dict:
    if c["status"] == "paid":
        raise HTTPException(409, f"{c['id']} is already paid.")
    c["status_before_freeze"] = c["status"] if c["status"] != "frozen" else c.get("status_before_freeze")
    c["status"] = "frozen"
    c["resolve_votes"] = []
    store.add_event(deal, role, "DISPUTED", f"{role.title()} disputed {c['id']} ({c['deliverable']}).", clause=c["id"])
    store.add_event(deal, "agent", "FROZEN",
                    f"Froze {_gbp(c['amount'])} for {c['id']}. The agent does not judge disputes: funds stay held "
                    f"until both parties agree. Contract says: '{c['deliverable']}', evidence required: "
                    f"{c['evidence']['description']}.", clause=c["id"], rule="P4", evidence=c["evidence_found"])
    store.save(deal)
    return deal


@app.post("/clause/{cid}/resolve")
def resolve(cid: str, body: Role):
    """Both parties must click resolve to unfreeze (P4)."""
    deal = _deal()
    c = store.clause(deal, cid)
    role = _role(body.role)
    if c["status"] != "frozen":
        raise HTTPException(409, f"{cid} is not frozen.")
    if role not in c["resolve_votes"]:
        c["resolve_votes"].append(role)
        store.add_event(deal, role, "RESOLVE_VOTE", f"{role.title()} agreed to lift the dispute on {cid}.", clause=cid)
    if set(c["resolve_votes"]) == ROLES:
        c["status"] = c.pop("status_before_freeze", None) or "pending"
        store.add_event(deal, "agent", "UNFROZEN", f"Both parties agreed; {cid} is back to '{c['status']}'.",
                        clause=cid, rule="P4")
    store.save(deal)
    return deal


@app.post("/deal/fast_forward")
def fast_forward(body: FastForward):
    """Demo clock: jump ahead and auto-release verified clauses whose window passed (P3)."""
    deal = _deal()
    deal["clock_offset_days"] += body.days
    store.add_event(deal, "system", "CLOCK", f"Demo clock moved forward {body.days} days.")
    store.save(deal)
    for c in deal["clauses"]:
        if rules.auto_release_due(deal, c):
            _release(deal, c, "auto_release", "timer")
    return store.load()


# ---------------- 5. chat: manipulation + why ----------------

def _context(deal: dict) -> str:
    clauses = "\n".join(
        f"{c['id']}: {c['deliverable']}, {_gbp(c['amount'])}, evidence: {c['evidence']['description']}, "
        f"status: {c['status']}, transfer: {c['transfer_id']}" for c in deal["clauses"])
    events = "\n".join(
        f"[{e['ts']}] #{e['id']} {e['actor']} {e['type']}"
        f"{' clause=' + e['clause'] if e['clause'] else ''}{' rule=' + e['rule'] if e['rule'] else ''}"
        f"{' stripe=' + e['stripe_id'] if e['stripe_id'] else ''}: {e['detail']}"
        + (f" | evidence: PR #{e['evidence']['number']} merged={e['evidence']['merged']} CI={e['evidence']['ci']}"
           if e.get("evidence") and "number" in (e["evidence"] or {}) else "")
        for e in deal["events"] if e["type"] != "CHAT")
    return (f"POLICY:\n{policy_text()}\n\nCONTRACT {deal['deal_id']} '{deal['title']}' between client "
            f"{deal['client']} and contractor {deal['contractor']}, total {_gbp(deal['total'])}, "
            f"deposit paid: {deal['deposit'].get('paid')}\nOriginal terms: \"{deal['terms']}\"\n{clauses}\n\n"
            f"EVENT LOG:\n{events}")


def _why_fallback(deal: dict, clause_id: str | None) -> str:
    evs = [e for e in deal["events"] if e["type"] in ("PAYOUT", "RELEASE_REFUSED", "FROZEN", "EVIDENCE_REJECTED")
           and (not clause_id or e["clause"] == clause_id)]
    if not evs:
        return "No payment, refusal or freeze has been recorded yet."
    e = evs[-1]
    return f"[{e['ts']}] {e['detail']} Rule {e['rule']}: {POLICY.get(e['rule'], '')}" + \
        (f" Stripe: {e['stripe_id']}." if e["stripe_id"] else "")


@app.post("/chat")
def chat(body: Chat):
    deal = _deal()
    role = _role(body.role)
    store.add_event(deal, role, "CHAT", body.message)
    store.save(deal)
    intent = llm.classify(body.message)
    cid = intent.get("clause")
    context = _context(deal)

    if intent["intent"] == "release_request":
        open_clauses = [c for c in deal["clauses"] if c["status"] != "paid"]
        target = next((c for c in open_clauses if c["id"] == cid), open_clauses[0] if open_clauses else None)
        if target:
            missing = "the client clicking Accept" if target["status"] == "verified" else \
                f"{target['evidence']['description']} (current status: {target['status']})"
            facts = (f"Refused under P6: chat messages are not recorded approvals. {target['id']} "
                     f"({target['deliverable']}, {_gbp(target['amount'])}) still requires {missing}.")
        else:
            facts = "Refused under P6. Nothing is left to release; all clauses are paid."
        store.add_event(deal, "agent", "RELEASE_REFUSED", facts, clause=target and target["id"], rule="P6")
        store.save(deal)
        reply = llm.phrase_refusal(body.message, context, facts)
    elif intent["intent"] == "dispute":
        target = next((c for c in deal["clauses"] if c["id"] == cid and c["status"] != "paid"), None) or \
            next((c for c in deal["clauses"] if c["status"] not in ("paid", "frozen")), None)
        if not target:
            reply = "There is no unpaid clause to dispute."
        else:
            _freeze(deal, target, role)
            deal = store.load()
            reply = llm.explain(
                f"The {role} disputes {target['id']}. Explain neutrally that funds are frozen under P4, what the "
                f"contract says for this clause and what evidence exists, and that both parties must agree to unfreeze.",
                _context(deal), _why_fallback(deal, target["id"]))
    elif intent["intent"] == "why_question":
        reply = llm.explain(body.message, context, _why_fallback(deal, cid))
    else:
        reply = llm.explain(body.message, context, "I'm the deal's guarantor. Ask me why a payment was made, "
                                                   "refused or frozen.")
    deal = store.load()
    store.add_event(deal, "agent", "CHAT_REPLY", reply)
    store.save(deal)
    return {"intent": intent, "reply": reply, "deal": deal}
