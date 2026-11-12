"""Create one captured sandbox payment to refund. Merchant side: needs PayPal credentials.

    python scripts/seed_order.py 60.00

It creates an order, prints the approval link, waits while you approve it in a
browser as the sandbox PERSONAL account, captures it, and saves what PayPal
answered under evidence/sandbox/ with the buyer's details removed.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import httpx

BASE = os.environ.get("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")
EVIDENCE = Path(__file__).parent.parent / "evidence" / "sandbox"
PRIVATE = {"payer", "payment_source", "shipping", "links", "email_address", "name", "address"}


def scrub(value):
    """Drop every field that describes a person; keep ids, states, amounts and times."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in PRIVATE}
    if isinstance(value, list):
        return [scrub(item) for item in value]
    return value


def save(name: str, reply: httpx.Response) -> dict:
    body = reply.json()
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    record = {
        "source": "PayPal sandbox, measured",
        "request": f"{reply.request.method} {reply.request.url.path}",
        "status": reply.status_code,
        "paypal_debug_id": reply.headers.get("paypal-debug-id", ""),
        "body": scrub(body),
    }
    (EVIDENCE / f"{name}.json").write_text(json.dumps(record, indent=1) + "\n")
    reply.raise_for_status()
    return body


def main() -> None:
    amount = sys.argv[1]
    http = httpx.Client(base_url=BASE, timeout=30.0)
    token = (
        http.post(
            "/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        )
        .raise_for_status()
        .json()["access_token"]
    )
    http.headers.update({"Authorization": f"Bearer {token}"})

    order = http.post(
        "/v2/checkout/orders",
        headers={"PayPal-Request-Id": str(uuid.uuid4())},
        json={
            "intent": "CAPTURE",
            "purchase_units": [{"amount": {"currency_code": "USD", "value": amount}}],
            "payment_source": {
                "paypal": {
                    "experience_context": {
                        "return_url": "https://example.com/paid",
                        "cancel_url": "https://example.com/cancelled",
                    }
                }
            },
        },
    )
    order_id = save("order-created", order)["id"]
    link = next(x["href"] for x in order.json()["links"] if x["rel"] == "payer-action")
    print(f"\nApprove as the sandbox PERSONAL account:\n  {link}\n")
    # Ask PayPal, rather than trusting the Enter key: capturing an order the buyer has
    # not approved yet is refused with 422 ORDER_NOT_APPROVED.
    while True:
        input("Press Enter here once the browser shows example.com/paid ... ")
        status = http.get(f"/v2/checkout/orders/{order_id}").raise_for_status().json()["status"]
        if status == "APPROVED":
            break
        print(f"PayPal reports this order as {status}, not APPROVED. Finish in the browser.")

    save(
        f"order-captured-{order_id}",
        http.post(
            f"/v2/checkout/orders/{order_id}/capture",
            headers={"PayPal-Request-Id": str(uuid.uuid4()), "Content-Type": "application/json"},
        ),
    )
    # The payee is in the order record, not in the capture reply.
    after = save(f"order-after-capture-{order_id}", http.get(f"/v2/checkout/orders/{order_id}"))
    unit = after["purchase_units"][0]
    print(f"ORDER_ID={order_id}")
    print(f"CAPTURE_ID={unit['payments']['captures'][0]['id']}")
    print(f"PAYPAL_MERCHANT_ID={unit.get('payee', {}).get('merchant_id', '')}")


if __name__ == "__main__":
    main()
