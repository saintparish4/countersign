"""What a refund call must satisfy before PayPal is asked for anything.

Two OpenScript policies run in order, both deterministic and both fail-closed.

1. ToolFirewallPolicy with the rules below: the call's shape. Four arguments,
   nothing else, each matching a strict pattern, and a hard ceiling on amount.
2. RefundPolicy: the call's meaning, checked against PayPal's record of the
   order and the merchant's limits. It is the only source of the capture id.

Where each limit comes from: the order a session may touch is set by the
merchant's own system when it opens the session; captured and refunded amounts
come from PayPal; auto_max, hard_max and daily_auto_cap come from merchant
configuration. Nothing here reads customer text or model output except the four
arguments being judged.

The firewall compares amounts as floats, which is fine for a coarse ceiling.
Every comparison that decides money in RefundPolicy uses Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from openscript_sdk import BasePolicy, FailureMode, InterceptorDecision, ToolRules
from openscript_sdk.contracts.types import ActionContext

from countersign.paypal import facts_from_order
from countersign.store import Store

TOOL = "refund_order"
CALL_FIELDS = ("session_id", "order_id", "amount", "currency")
PATTERNS = {
    "session_id": r"[0-9a-f]{32}",
    "order_id": r"[A-Z0-9]{17,32}",
    "amount": r"(0|[1-9][0-9]{0,5})\.[0-9]{2}",
    "currency": r"[A-Z]{3}",
}
REFUNDABLE_CAPTURE = ("COMPLETED", "PARTIALLY_REFUNDED")


@dataclass(frozen=True)
class Limits:
    """Merchant configuration. The model cannot read or change it."""

    currency: str = "USD"
    auto_max: Decimal = Decimal("25.00")  # at or below this, no person is asked
    hard_max: Decimal = Decimal("200.00")  # above this, refused even with a person
    daily_auto_cap: Decimal = Decimal("100.00")  # total per UTC day without a person
    merchant_id: str = ""  # the order's payee must be this account; "" skips the check


def tool_rules(limits: Limits) -> ToolRules:
    args = {name: {"required": True, "pattern": PATTERNS[name]} for name in CALL_FIELDS}
    args["amount"] |= {"min": 0.01, "max": float(limits.hard_max)}
    args["currency"] = {"required": True, "one_of": [limits.currency]}
    return ToolRules.model_validate(
        {"default_deny": True, "rules": {TOOL: {"unlisted_args": "deny", "args": args}}}
    )


class RefundPolicy(BasePolicy):
    """Bind the refund to the session's order and to PayPal's record of it."""

    failure_mode = FailureMode.FAIL_CLOSED

    def __init__(self, paypal: object, store: Store, limits: Limits) -> None:
        self._paypal, self._store, self._limits = paypal, store, limits

    async def before_action(self, context: ActionContext) -> ActionContext:
        args = context.input_data["args"]
        amount = Decimal(args["amount"])
        bound = self._store.session_order(args["session_id"])
        if bound is None:
            return _deny(context, "unknown_session")
        if args["order_id"] != bound:
            return _deny(context, "order_not_bound_to_session")
        if amount > self._limits.hard_max:
            return _deny(context, "above_hard_limit")

        facts = facts_from_order(await self._paypal.get_order(bound))
        if facts.order_id != bound:
            return _deny(context, "order_record_mismatch")
        if self._limits.merchant_id and facts.merchant_id != self._limits.merchant_id:
            return _deny(context, "order_belongs_to_another_merchant")
        if facts.status != "COMPLETED" or facts.capture_status not in REFUNDABLE_CAPTURE:
            return _deny(context, "order_not_refundable")
        if args["currency"] != facts.currency:
            return _deny(context, "currency_mismatch")
        if amount > facts.refundable:
            return _deny(context, "exceeds_refundable_amount")

        # Overwritten on every call, so the capture is never one the caller chose.
        # It joins the arguments before an approval is hashed, which binds a
        # person's yes to this capture as well as to this amount.
        args["capture_id"] = facts.capture_id
        context.metadata["refund"] = {
            "captured": f"{facts.captured:.2f}",
            "refunded": f"{facts.refunded:.2f}",
        }
        spent = sum((Decimal(a) for a in self._store.auto_refunds_today()), Decimal("0"))
        if amount > self._limits.auto_max:
            return _hold(context, "above_auto_limit")
        if spent + amount > self._limits.daily_auto_cap:
            return _hold(context, "daily_auto_cap_reached")
        return context


def _deny(context: ActionContext, code: str) -> ActionContext:
    context.decision, context.decision_reason = InterceptorDecision.DENY, code
    return context


def _hold(context: ActionContext, code: str) -> ActionContext:
    context.decision, context.decision_reason = InterceptorDecision.REQUIRE_APPROVAL, code
    return context
