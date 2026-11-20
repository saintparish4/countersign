"""Glue between the demo page and the gateway. Demo-only; not part of Countersign.

It builds the same gateway `evals/offline.py` builds (the real Gateway, the real
PayPal client, the fake PayPal from tests/fakes.py), opens one support session
per order, and hands the page JSON. Every decision the page shows is returned
by `Gateway.refund` or `Gateway.decide`; nothing is decided here.
"""

from __future__ import annotations

import json
from decimal import Decimal

from tests.fakes import make_gateway

# order id, captured, already refunded before the demo starts
ORDERS = (
    ("ORDER00000000000A", "60.00", "0.00"),
    ("ORDER00000000000B", "500.00", "0.00"),
    ("ORDER00000000000C", "180.00", "0.00"),
    ("ORDER00000000000D", "95.00", "40.00"),
    ("ORDER00000000000E", "240.00", "0.00"),
    ("ORDER00000000000F", "32.50", "0.00"),
    ("ORDER00000000000G", "120.00", "0.00"),
    ("ORDER00000000000H", "75.00", "0.00"),
    ("ORDER00000000000J", "64.00", "0.00"),
    ("ORDER00000000000K", "210.00", "0.00"),
    ("ORDER00000000000L", "48.00", "0.00"),
    ("ORDER00000000000M", "150.00", "0.00"),
    ("ORDER00000000000N", "89.99", "0.00"),
    ("ORDER00000000000P", "300.00", "0.00"),
    ("ORDER00000000000Q", "54.00", "0.00"),
    ("ORDER00000000000R", "135.00", "0.00"),
)
# The merchant's helpdesk, which is not part of Countersign: what the customer wrote
# on each ticket, and the amount they asked for. The page shows it to staff beside a
# held refund. It never passes through the agent's request, the gateway or the audit
# trail, and the gateway decides nothing from it.
TICKETS = {
    "A": ("12.00", "One of the two mugs in my order arrived cracked. It cost 12.00. Can I get that back?"),
    "B": ("150.00", "The espresso machine works, but the milk frother that was part of the bundle (150.00) was missing from the box. I'd like a refund for that part."),
    "C": ("80.00", "One of the three lamps arrived shattered. They were 60.00 each. I think 80.00 is fair for the lamp and the hassle."),
    "D": ("60.00", "You already refunded 40.00 for the late delivery, but the jacket doesn't fit either. Please refund 60.00 more."),
    "E": ("240.00", "The whole order never arrived. Refund all 240.00."),
    "F": ("18.00", "The phone case came in the wrong colour. It was 18.00."),
    "G": ("60.00", "Two of the four chair cushions arrived stained. They were 30.00 each, so 60.00 back please."),
    "H": ("9.99", "I was charged 9.99 for gift wrap and the parcel came unwrapped."),
    "J": ("64.00", "The headphones stopped charging after two days. I have posted them back. Full refund of 64.00 please."),
    "K": ("105.00", "Only one of the two monitors was delivered. Please refund the missing one, 105.00."),
    "L": ("24.00", "One of the two candles arrived melted. It was 24.00."),
    "M": ("45.00", "The keyboard came without the wrist rest that was listed at 45.00."),
    "N": ("15.00", "Shipping was meant to be free and I was charged 15.00."),
    "P": ("120.00", "The rug is a size smaller than I ordered. I'll keep it if you refund the difference, 120.00."),
    "Q": ("21.50", "One book in the set arrived with a torn cover. It was 21.50."),
    "R": ("135.00", "I returned the coat last week and tracking shows it was delivered. Please refund 135.00."),
}
# (the ticket's order, the order asked for, amount): what the agent asks before you arrive
SEED = (
    ("ORDER00000000000A", "ORDER00000000000A", "12.00"),
    ("ORDER00000000000A", "ORDER00000000000A", "12.00"),
    ("ORDER00000000000A", "ORDER00000000000B", "20.00"),
    ("ORDER00000000000B", "ORDER00000000000B", "150.00"),
    ("ORDER00000000000C", "ORDER00000000000C", "80.00"),
    ("ORDER00000000000E", "ORDER00000000000E", "240.00"),
    ("ORDER00000000000D", "ORDER00000000000D", "60.00"),
)

gateway = fake = store = sessions = None


async def reset() -> str:
    global gateway, fake, store, sessions
    gateway, fake, store = make_gateway()
    sessions = {}
    for order_id, captured, refunded in ORDERS:
        fake.add_order(order_id, captured, refunded)
        sessions[order_id] = gateway.open_session(order_id)
    for ticket, order_id, amount in SEED:
        await ask(ticket, order_id, amount)
    return snapshot()


async def ask(ticket: str, order_id: str, amount: str) -> str:
    """One request with the agent's credential: POST /v1/refunds."""
    call = {
        "session_id": sessions.get(ticket, ""),
        "order_id": order_id,
        "amount": amount,
        "currency": gateway.limits.currency,
    }
    return json.dumps({"request": call, "reply": await gateway.refund(call)})


async def decide(approval_id: str, approve: bool) -> str:
    """One decision with the operator's credential: POST /v1/approvals/{id}."""
    return json.dumps(await gateway.decide(approval_id, approve=approve))


def snapshot() -> str:
    audit = gateway.audit()
    held_for = {row["approval_id"]: row["reason"] for row in audit if row["event"] == "held"}
    pending = [
        dict(item, reason=held_for.get(item["approval_id"], "")) for item in gateway.pending()
    ]
    orders = []
    for order_id, captured, _ in ORDERS:
        refunded = fake.refunded_total(order_id)
        orders.append(
            {
                "order_id": order_id,
                "captured": captured,
                "refunded": f"{refunded:.2f}",
                "intent": (store.intent(order_id) or {"state": ""})["state"],
                "wants": TICKETS[order_id[-1]][0],
                "customer_wrote": TICKETS[order_id[-1]][1],
            }
        )
    limits = gateway.limits
    return json.dumps(
        {
            "limits": {
                "currency": limits.currency,
                "auto_max": f"{limits.auto_max:.2f}",
                "hard_max": f"{limits.hard_max:.2f}",
                "daily_auto_cap": f"{limits.daily_auto_cap:.2f}",
            },
            "orders": orders,
            "pending": pending,
            "audit": audit,
            "auto_sent_today": f"{sum(map(Decimal, store.auto_refunds_today()), Decimal('0')):.2f}",
            "paypal_refund_requests": len(fake.requests),
            "paypal_refunds_created": len(fake.applied),
            "paypal_refunded_total": f"{sum((Decimal(r['refund']['amount']['value']) for r in fake.applied.values()), Decimal('0')):.2f}",
        }
    )
