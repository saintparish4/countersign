"""The reconciliation rule, on hand-built records, since the script itself needs PayPal."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from reconcile import compare  # noqa: E402

ORDER = "ORDER00000000000A"


def row(event: str, refund_id: str = "", amount: str = "12.00") -> dict:
    return {"order_id": ORDER, "event": event, "refund_id": refund_id, "amount": amount}


def on_paypal(refund_id: str, value: str, status: str = "COMPLETED") -> dict:
    return {"id": refund_id, "status": status, "amount": {"value": value}}


def test_matching_records_agree():
    assert compare(ORDER, [row("sent"), row("refunded", "R1")], [on_paypal("R1", "12.00")]) == []
    assert compare(ORDER, [row("denied")], []) == []


def test_every_kind_of_disagreement_is_reported():
    assert compare(ORDER, [row("denied")], [on_paypal("R9", "5.00")])
    assert compare(ORDER, [row("refunded", "R1")], [])
    assert compare(ORDER, [row("refunded", "R1")], [on_paypal("R1", "60.00")])
    assert compare(ORDER, [row("refunded", "R1")], [on_paypal("R1", "12.00", "CANCELLED")])
    assert compare(ORDER, [row("sent")], [])
    assert compare(ORDER, [row("sent"), row("unknown")], []) == []
