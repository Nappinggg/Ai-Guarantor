"""Permanent guarantor policy. Parties cannot change these; every money decision cites one."""

POLICY = {
    "P1": "Pay only the contractor's connected account named in the confirmed contract.",
    "P2": "Total payouts never exceed the client's deposit for the deal.",
    "P3": "Release a clause only when its evidence is verified and the client has accepted "
          "(or the auto-release window passed with no objection).",
    "P4": "A dispute freezes that clause's funds until both parties agree.",
    "P5": "The contract changes only with explicit confirmation from both parties.",
    "P6": "Act only on recorded events (button clicks, API checks), never on chat claims.",
}


def policy_text() -> str:
    return "\n".join(f"{k}: {v}" for k, v in POLICY.items())
