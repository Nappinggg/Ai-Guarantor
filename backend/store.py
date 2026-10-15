"""Deal state + append-only event log in a JSON file. Source of truth and the agent's memory."""
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "deal.json"
_lock = threading.Lock()


def now(deal: dict | None = None) -> datetime:
    """Demo clock: real time plus any fast-forwarded days."""
    offset = deal.get("clock_offset_days", 0) if deal else 0
    return datetime.now() + timedelta(days=offset)


def ts(deal: dict | None = None) -> str:
    return now(deal).strftime("%Y-%m-%d %H:%M:%S")


def load() -> dict | None:
    with _lock:
        if not DATA.exists():
            return None
        return json.loads(DATA.read_text(encoding="utf-8"))


def save(deal: dict) -> None:
    with _lock:
        DATA.parent.mkdir(exist_ok=True)
        DATA.write_text(json.dumps(deal, indent=2), encoding="utf-8")


def reset() -> None:
    with _lock:
        DATA.unlink(missing_ok=True)


def new_deal_id() -> str:
    return f"deal_{int(time.time())}"


def add_event(deal: dict, actor: str, type_: str, detail: str, *, clause: str | None = None,
              rule: str | None = None, stripe_id: str | None = None, evidence: dict | None = None) -> dict:
    event = {
        "id": len(deal["events"]) + 1,
        "ts": ts(deal),
        "actor": actor,
        "type": type_,
        "clause": clause,
        "rule": rule,
        "detail": detail,
        "stripe_id": stripe_id,
        "evidence": evidence,
    }
    deal["events"].append(event)
    return event


def clause(deal: dict, clause_id: str) -> dict:
    for c in deal["clauses"]:
        if c["id"] == clause_id:
            return c
    raise KeyError(clause_id)
