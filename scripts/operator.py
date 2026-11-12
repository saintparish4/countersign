"""The merchant's side of the gateway, from a terminal. Needs the operator token.

python scripts/operator.py open ORDER_ID      open a support session for an order
python scripts/operator.py pending            what is waiting for a person
python scripts/operator.py approve ID         run that exact refund
python scripts/operator.py reject ID
python scripts/operator.py audit              the audit trail
"""

from __future__ import annotations

import json
import os
import sys

import httpx

URL = os.environ.get("COUNTERSIGN_URL", "http://127.0.0.1:8787")
AUTH = {"Authorization": "Bearer " + os.environ["COUNTERSIGN_OPERATOR_TOKEN"]}


def call(method: str, path: str, body: dict | None = None):
    reply = httpx.request(method, URL + path, headers=AUTH, json=body, timeout=60.0)
    reply.raise_for_status()
    return reply.json()


def main() -> None:
    command, argument = sys.argv[1], (sys.argv[2:] or [""])[0]
    if command == "open":
        print(call("POST", "/v1/sessions", {"order_id": argument})["session_id"])
    elif command == "pending":
        for item in call("GET", "/v1/approvals"):
            left = float(item["captured"]) - float(item["refunded"])
            print(
                f"\nAPPROVAL {item['approval_id']}\n"
                f"  refund    {item['amount']} {item['currency']}\n"
                f"  order     {item['order_id']}\n"
                f"  PayPal    captured {item['captured']}, already refunded {item['refunded']},"
                f" {left:.2f} left\n"
                f"  session   {item['session_id']}\n"
                "  Every figure above is from PayPal or the gateway. None of it is text the\n"
                "  customer or the model wrote."
            )
    elif command in ("approve", "reject"):
        print(
            json.dumps(call("POST", f"/v1/approvals/{argument}", {"approve": command == "approve"}))
        )
    elif command == "audit":
        for row in call("GET", "/v1/audit"):
            extras = " ".join(
                f"{key}={row[key]}"
                for key in ("reason", "approval_id", "request_id", "refund_id", "paypal_status")
                if row[key]
            )
            print(
                f"{row['seq']:>3} {row['at']} {row['actor']:<8} {row['event']:<9}"
                f" {row['order_id']:<18} {row['amount']:>8} {row['currency']:<3} {extras}"
            )
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
