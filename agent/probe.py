"""Run from the agent's environment: can this process move money without the gateway?

It tries what a hijacked agent would try. Each line should say BLOCKED. Exit
status is 0 only if every attempt was blocked.

    python -m agent.probe

What this shows: the agent's environment holds nothing PayPal accepts, and its
own token is not a PayPal credential or an operator credential. What it cannot
show: that the two processes are isolated from each other. Run both as one OS
user on one machine and the agent could read the gateway's .env file. That
separation is a deployment property (see docker-compose.yml).
"""

from __future__ import annotations

import os
import sys

import httpx

PAYPAL = os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
REFUND = PAYPAL + "/v2/payments/captures/0AB12345CD678901E/refund"
BODY = {"amount": {"value": "1.00", "currency_code": "USD"}}


def main() -> int:
    token = os.environ.get("COUNTERSIGN_AGENT_TOKEN", "")
    gateway = os.environ.get("COUNTERSIGN_URL", "")
    held = sorted(k for k in os.environ if k.startswith("PAYPAL_") and k != "PAYPAL_BASE_URL")
    checks = [("no PAYPAL_* credential in this environment", not held, ", ".join(held) or "none")]

    attempts = [
        ("PayPal refund, no credentials", "POST", REFUND, {}, BODY),
        ("PayPal refund, agent token as bearer", "POST", REFUND,
         {"Authorization": f"Bearer {token}"}, BODY),
        ("PayPal OAuth, agent token as secret", "POST", PAYPAL + "/v1/oauth2/token",
         {"Authorization": f"Basic {token}"}, None),
    ]  # fmt: skip
    if gateway:
        agent = {"Authorization": f"Bearer {token}"}
        attempts += [
            ("gateway: open a session for any order", "POST", gateway + "/v1/sessions", agent,
             {"order_id": "ORDER00000000000B"}),
            ("gateway: list approvals", "GET", gateway + "/v1/approvals", agent, None),
            ("gateway: approve a refund", "POST", gateway + "/v1/approvals/anything", agent,
             {"approve": True}),
        ]  # fmt: skip
    for name, method, url, headers, body in attempts:
        try:
            reply = httpx.request(method, url, headers=headers, json=body, timeout=20.0)
            checks.append((name, reply.status_code in (401, 403), f"http {reply.status_code}"))
        except httpx.HTTPError as exc:
            checks.append((name, False, f"no answer: {type(exc).__name__}"))

    for name, blocked, detail in checks:
        print(f"{'BLOCKED' if blocked else 'OPEN   '}  {name}  [{detail}]")
    return 0 if all(blocked for _, blocked, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())