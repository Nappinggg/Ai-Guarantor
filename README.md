# 🛡️ AI Guarantor

**A neutral AI escrow agent that obeys the contract, not whoever is giving orders.**

Track 01, *Agents that spend*. All money moves in **Stripe test mode**.

Two strangers want to do a deal: a client and a freelancer. The client fears paying for nothing, and the freelancer fears working for nothing. AI Guarantor sits in the middle:

1. Both parties describe the deal in plain words. The agent turns it into a contract made of **clauses**, each with an amount and the **evidence** that proves it was delivered.
2. Both approve, and the contract becomes the agent's rules.
3. The client deposits the full amount through **Stripe Checkout**. The money is held on the platform.
4. The contractor delivers, for example by merging a GitHub PR. The agent **verifies the evidence** through the GitHub API and summarises it for the client.
5. The client accepts, and the agent releases that clause's money with a **Stripe Connect transfer**.
6. Anyone can ask *"why did you pay this?"* and get the clause, the rule, the evidence, the timestamp and the Stripe ID.

## The key idea: the AI explains, the code decides

The LLM (Gemini) **translates** plain terms into a contract and **explains** decisions. It never decides to pay. Every payout goes through a deterministic policy engine ([`backend/rules.py`](backend/rules.py)) that checks the permanent policy:

| Rule | Permanent policy (parties can't change it) |
|---|---|
| **P1** | Pay only the contractor's connected account named in the confirmed contract. |
| **P2** | Total payouts never exceed the client's deposit. |
| **P3** | Release a clause only when its evidence is verified **and** the client accepted (or the auto-release window passed with no objection). |
| **P4** | A dispute freezes that clause's funds until both parties agree. |
| **P5** | The contract changes only with explicit confirmation from both parties. |
| **P6** | Act only on recorded events (button clicks, API checks), never on chat claims. |

So *"the client said it's fine, just release the rest"* is refused under **P6**, and a dispute **freezes** the money (**P4**). The agent never judges a dispute itself; it shows both sides what the contract says.

## Every pound is explainable

Every action is written to an append-only **event log** (actor, clause, rule, evidence, Stripe ID). The *"why?"* answers are built only from that log, the contract and the policy. For example:

> *I paid Bob £250.00 for C1 (Homepage mockup) at 19:41:38 under rule P3: PR #1 was merged, and the client accepted at 19:41:37. Stripe transfer tr_3UQ…*

## Stripe usage (test mode)

| Step | Stripe |
|---|---|
| Client deposit | Checkout Session (`transfer_group` = deal) |
| Holding funds | Platform balance. Nothing is transferred until the rules pass. |
| Release per clause | `Transfer` to the contractor's connected account, with `source_transaction` = deposit charge |
| Dispute freeze | No transfer is made |

## Architecture

```
static/index.html      single page: Client / Contractor toggle, contract, chat, audit log
backend/app.py         FastAPI endpoints + chat (intent → rules → reply)
backend/rules.py       deterministic policy engine (the ONLY path to a payout)
backend/stripe_svc.py  Checkout, deposits, Connect transfers
backend/github_svc.py  PR merged? CI status? (evidence)
backend/llm.py         Gemini: terms→contract, evidence summary, explanations (with fallbacks)
backend/store.py       deal state + event log (JSON)
```

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # macOS/Linux: .venv/bin/pip
cp .env.example .env                                # add sk_test_ key, connected account, Gemini key
.venv/Scripts/python -m uvicorn backend.app:app --port 8000
```

Open http://127.0.0.1:8000. For the deposit, use test card `4000 0000 0000 0077`, which makes funds available immediately.

The demo evidence repo is [Nappinggg/guarantor-demo-site](https://github.com/Nappinggg/guarantor-demo-site): PR #1 (merged) is the mockup clause, and PR #3 (open) is the launch clause.

## Not built (yet)

Refunds, partial-amount proposals, more evidence types (shipping tracking, file review), and multiple deals at once. The contract model already supports any deal shaped as *who pays → who receives → how much → for what → what proves it*.
