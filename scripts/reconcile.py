"""Compare the gateway's audit trail with PayPal's own records, order by order.

    python scripts/reconcile.py

Needs the operator token (to read the audit trail) and the PayPal credentials
(to read each order). For every order the trail mentions, the refunds PayPal
holds must be exactly the trail's "refunded" rows: same refund id, same
amount, nothing extra on either side. Exit status 0 only if every order agrees.

It also saves each order as PayPal reports it, with the buyer's details
removed, under evidence/sandbox/.

What it cannot see: a refund made for an order the trail never mentions, for
example one issued by hand in the PayPal dashboard for an unrelated order.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from seed_order import BASE, save  # noqa: E402


def compare(order_id: str, trail: list[dict], paypal_refunds: list[dict]) -> list[str]:
    """Problems for one order; an empty list means the two records agree."""
    ours = {
        row["refund_id"]: row["amount"]
        for row in trail
        if row["order_id"] == order_id and row["event"] == "refunded"
    }
    theirs = {
        refund["id"]: refund["amount"]["value"]
        for refund in paypal_refunds
        if refund.get("status") in ("COMPLETED", "PENDING")
    }
    problems = [f"PayPal has refund {r} ({a}) with no audit row" for r, a in theirs.items()
                if r not in ours]  # fmt: skip
    problems += [f"audit says refund {r} ({a}) but PayPal has none" for r, a in ours.items()
                 if r not in theirs]  # fmt: skip
    problems += [f"refund {r}: audit {ours[r]} vs PayPal {theirs[r]}" for r in ours
                 if r in theirs and ours[r] != theirs[r]]  # fmt: skip
    sent = sum(1 for row in trail if row["order_id"] == order_id and row["event"] == "sent")
    if (
        sent
        and not ours
        and not any(
            row["order_id"] == order_id and row["event"] in ("failed", "unknown") for row in trail
        )
    ):
        problems.append("a refund was sent and its outcome was never recorded")
    return problems


def main() -> int:
    gateway = os.environ.get("COUNTERSIGN_URL", "http://127.0.0.1:8787")
    trail = (
        httpx.get(
            gateway + "/v1/audit",
            headers={"Authorization": "Bearer " + os.environ["COUNTERSIGN_OPERATOR_TOKEN"]},
            timeout=30.0,
        )
        .raise_for_status()
        .json()
    )
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

    agree = True
    for order_id in sorted({row["order_id"] for row in trail if row["order_id"]}):
        reply = http.get(f"/v2/checkout/orders/{order_id}")
        if reply.status_code == 404:
            print(f"SKIP   {order_id}: PayPal has no such order (a refused request named it)")
            continue
        order = save(f"reconcile-{order_id}", reply)
        refunds = order["purchase_units"][0].get("payments", {}).get("refunds", [])
        problems = compare(order_id, trail, refunds)
        agree = agree and not problems
        on_paypal = [f"{r['id']}={r['amount']['value']}" for r in refunds] or ["no refunds"]
        print(f"{'AGREE ' if not problems else 'DIFFER'} {order_id}: PayPal {', '.join(on_paypal)}")
        for problem in problems:
            print("       " + problem)
    return 0 if agree else 1


if __name__ == "__main__":
    sys.exit(main())
