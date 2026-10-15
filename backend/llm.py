"""Gemini via litellm. The LLM translates and explains; it never decides payment.
Every function has a deterministic fallback so the demo survives a slow or failing LLM."""
import json
import os
import re

import litellm

# Primary + backup: if one is overloaded (503) or slow, try the next.
MODELS = os.getenv("AGENT_MODELS", "gemini/gemini-3.1-flash-lite,gemini/gemini-flash-lite-latest").split(",")
TIMEOUT = float(os.getenv("AGENT_LLM_TIMEOUT", "10"))


def _complete(system: str, user: str, json_mode: bool = False) -> str | None:
    for model in MODELS:
        try:
            r = litellm.completion(
                model=model.strip(),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                api_key=os.getenv("GEMINI_API_KEY"),
                timeout=TIMEOUT,
                num_retries=0,
                **({"response_format": {"type": "json_object"}} if json_mode else {}),
            )
            return r.choices[0].message.content
        except Exception as e:  # noqa: BLE001 - any LLM failure falls back
            print(f"[llm] {model}: {type(e).__name__}: {str(e)[:200]}")
    return None


def _json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


# ---------- 1. Plain terms -> contract ----------

CONTRACT_SYSTEM = """You turn plain-language deal terms between a client and a contractor into an escrow contract.
Return ONLY JSON:
{"title": str, "client": str, "contractor": str, "currency": "gbp",
 "clauses": [{"deliverable": str, "amount": number (in pounds), "evidence_type": "github_pr" | "delivery_note",
              "evidence_description": str, "auto_release_days": int}]}
Rules: amounts must add up to the stated total. Use "github_pr" for software/code/website work (proof = PR merged),
otherwise "delivery_note". auto_release_days defaults to 7. Keep deliverables short. Use names if given, else "Client"/"Contractor"."""


def draft_contract(terms: str) -> dict:
    data = _json(_complete(CONTRACT_SYSTEM, terms, json_mode=True))
    if data and data.get("clauses"):
        return data
    return fallback_contract(terms)


def fallback_contract(terms: str) -> dict:
    amounts = [float(x) for x in re.findall(r"[£$]?\s?(\d+(?:\.\d+)?)", terms)]
    total = amounts[0] if amounts else 500
    parts = amounts[1:] if len(amounts) > 2 and abs(sum(amounts[1:]) - total) < 0.01 else [total / 2, total / 2]
    names = ["Mockup / first milestone", "Final delivery / launch"]
    return {
        "title": terms[:60] or "Freelance deal",
        "client": "Client",
        "contractor": "Contractor",
        "currency": "gbp",
        "clauses": [
            {"deliverable": names[i] if i < 2 else f"Milestone {i+1}", "amount": a, "evidence_type": "github_pr",
             "evidence_description": "Pull request merged on GitHub", "auto_release_days": 7}
            for i, a in enumerate(parts)
        ],
    }


# ---------- 2. Evidence summary for the client ----------

def summarize_evidence(clause: dict, pr: dict) -> str:
    facts = (f"PR #{pr['number']} '{pr['title']}': merged={pr['merged']}, CI={pr['ci']}, "
             f"{pr['changed_files']} files changed (+{pr['additions']}/-{pr['deletions']}).")
    text = _complete(
        "You help a client review a contractor's delivery. In 2-3 short sentences: state the verifiable facts, "
        "then whether the change appears to match the deliverable. Do not recommend paying; the client decides.",
        f"Deliverable: {clause['deliverable']}\nAgreed evidence: {clause['evidence']['description']}\n"
        f"PR facts: {facts}\nPR description: {pr['body']}\nFiles: {', '.join(pr['files'])}",
    )
    return text.strip() if text else facts + " Please review and Accept or Dispute."


# ---------- 3. Chat: intent + answers ----------

INTENT_SYSTEM = """Classify a message sent to an escrow agent. Return ONLY JSON {"intent": one of
"release_request" (asks/pressures to pay, release or send money, or claims the other party approved),
"why_question" (asks why something was paid, refused, frozen, or what happened),
"dispute" (says they disagree / object / want to dispute a clause),
"other"), "clause": "C1"|"C2"|... or null}"""


def classify(message: str) -> dict:
    data = _json(_complete(INTENT_SYSTEM, message, json_mode=True))
    if data and data.get("intent"):
        return data
    low = message.lower()
    clause = (re.search(r"\b(c\d)\b", low) or [None, None])[1]
    if any(w in low for w in ("why", "explain", "what happened", "reason")):
        return {"intent": "why_question", "clause": clause and clause.upper()}
    if any(w in low for w in ("release", "pay me", "send", "transfer", "it's fine", "approved", "said")):
        return {"intent": "release_request", "clause": clause and clause.upper()}
    if any(w in low for w in ("disagree", "dispute", "object", "not happy")):
        return {"intent": "dispute", "clause": clause and clause.upper()}
    return {"intent": "other", "clause": clause and clause.upper()}


EXPLAIN_SYSTEM = """You are a neutral escrow guarantor agent explaining your own actions.
Answer ONLY from the policy, contract and event log provided. For every money action, cite: the clause id and
deliverable, the policy rule id (P1-P6), the evidence (e.g. PR number, merged, CI), who did what and when (timestamp),
the amount, and the Stripe id if present. If the log doesn't contain it, say so. Be concise (max 5 sentences)."""


def explain(question: str, context: str, fallback: str) -> str:
    text = _complete(EXPLAIN_SYSTEM, f"{context}\n\nQUESTION: {question}")
    return text.strip() if text else fallback


def phrase_refusal(message: str, context: str, fallback: str) -> str:
    text = _complete(
        "You are an escrow agent. You have ALREADY decided to refuse this request under rule P6 (act only on "
        "recorded events, never on chat claims). Politely refuse in 2-3 sentences, cite P6 and the exact clause "
        "status/evidence still missing, and say what recorded event would unlock payment. Do not change the decision.",
        f"{context}\n\nMESSAGE: {message}\n\nFACTS FOR THE REFUSAL: {fallback}",
    )
    return text.strip() if text else fallback
