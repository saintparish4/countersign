"""One refund request from start to finish: decide, send at most once, write it down.

The refund runs as an OpenScript SecureTool, so the policies in policy.py are
the only way to reach `_send`, and `_send` is the only code that asks PayPal
for money. One lock makes requests take turns: the gateway is a single process
by design, which is what lets "one intent per order" be a plain row check.

Rules this module adds on top of the policies:

- An order gets one refund intent, ever. The same request again returns what
  happened the first time. A different amount for that order is refused.
- A held request is run by the person's approval, with the stored arguments.
  The agent never redeems an approval.
- When PayPal's answer is unknown, the retry sends the stored request again
  under the stored PayPal-Request-Id, and only within RESEND_WINDOW seconds.
  After that a person has to look.
- Anything unexpected ends as "error" and sends nothing further.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from openscript_sdk import ActionBlockedError, InMemoryApprovalStore, SecureTool, ToolFirewallPolicy

from countersign.paypal import (
    COUNTS_AS_REFUNDED,
    PayPalRejected,
    PayPalUnknown,
    facts_from_order,
)
from countersign.policy import CALL_FIELDS, PATTERNS, TOOL, Limits, RefundPolicy, tool_rules
from countersign.store import Store, now, short

RESEND_WINDOW = 900


class Gateway:
    def __init__(self, paypal: object, store: Store, limits: Limits) -> None:
        self._paypal, self._store, self.limits = paypal, store, limits
        self._approvals = InMemoryApprovalStore()
        self._lock = asyncio.Lock()
        self._tool = SecureTool(
            self._send,
            policies=[
                ToolFirewallPolicy(rules=tool_rules(limits)),
                RefundPolicy(paypal, store, limits),
            ],
            name=TOOL,
            approval_store=self._approvals,
        )

    # -- what the agent can ask for ------------------------------------------

    async def refund(self, call: object) -> dict:
        async with self._lock:
            if not isinstance(call, dict) or set(call) != set(CALL_FIELDS):
                return self._out("denied", {}, reason="malformed")
            prior = self._store.intent(call["order_id"])
            mine = self._store.session_order(call["session_id"]) == call["order_id"]
            if prior is not None and mine:
                return await self._again(prior, call)
            return await self._attempt(call, approval_id="")

    async def session_facts(self, session_id: str) -> dict:
        order_id = self._store.session_order(session_id)
        if order_id is None:
            raise LookupError("unknown session")
        facts = facts_from_order(await self._paypal.get_order(order_id))
        return {
            "session_id": session_id,
            "order_id": order_id,
            "currency": facts.currency,
            "captured": f"{facts.captured:.2f}",
            "refundable": f"{facts.refundable:.2f}",
        }

    # -- what the merchant's side can do -------------------------------------

    def open_session(self, order_id: str) -> str:
        if re.fullmatch(PATTERNS["order_id"], order_id) is None:
            raise ValueError("not an order id")
        return self._store.open_session(order_id)

    def pending(self) -> list[dict]:
        keys = (*CALL_FIELDS, "approval_id", "captured", "refunded")
        return [{key: row[key] for key in keys} for row in self._store.held()]

    def audit(self) -> list[dict]:
        return self._store.audit()

    async def decide(self, approval_id: str, approve: bool) -> dict:
        async with self._lock:
            row = self._store.intent_for_approval(approval_id)
            if row is None:
                return {"outcome": "error", "reason": "no_such_pending_approval"}
            call = {key: row[key] for key in CALL_FIELDS}
            try:
                known = await self._approvals.decide(approval_id, approve, decided_by="operator")
            except ValueError:
                known = None
            if known is None:  # expired, or the gateway restarted and forgot it
                approve, reason = False, "approval_expired"
            else:
                reason = "approved_by_operator" if approve else "rejected_by_operator"
            if not approve:
                self._store.set_state(call["order_id"], "rejected")
                return self._out(
                    "rejected", call, actor="operator", reason=reason, approval_id=approval_id
                )
            self._store.record(
                "approved", actor="operator", reason=reason, approval_id=approval_id, **call
            )
            return await self._attempt(call, approval_id=approval_id)

    # -- the one path to PayPal ----------------------------------------------

    async def _send(
        self, session_id: str, order_id: str, amount: str, currency: str, capture_id: str = ""
    ) -> dict:
        """The guarded tool. SecureTool calls this only after every policy has passed."""
        if not capture_id:
            raise RuntimeError("no capture was resolved for this call")
        call = {
            "session_id": session_id,
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
        }
        request_id = self._store.begin(call, capture_id)
        self._store.record("sent", request_id=request_id, **call)
        return await self._paypal.refund(capture_id, amount, currency, request_id)

    async def _attempt(self, call: dict, approval_id: str) -> dict:
        order_id = call["order_id"]
        try:
            body = await self._tool.call(
                call,
                approval_id=approval_id,
                session_id=call["session_id"] if isinstance(call["session_id"], str) else "",
            )
        except ActionBlockedError as blocked:
            if blocked.approval_id:
                facts = blocked.context.metadata["refund"]
                self._store.hold(call, blocked.approval_id, facts["captured"], facts["refunded"])
                return self._out(
                    "held", call, reason=blocked.reason, approval_id=blocked.approval_id
                )
            ours = blocked.interceptor == RefundPolicy.__name__
            self._abandon_hold(order_id)
            return self._out("denied", call, reason=blocked.reason if ours else "firewall_rejected")
        except PayPalUnknown:
            self._store.set_state(order_id, "unknown")
            return self._out("unknown", call, reason="paypal_outcome_unknown")
        except PayPalRejected as refused:
            self._store.set_state(order_id, "failed")
            return self._out(
                "failed",
                call,
                reason=refused.issue,
                paypal_status=str(refused.status),
                debug_id=refused.debug_id,
            )
        except Exception:
            self._abandon_hold(order_id)
            return self._out("error", call, reason="gateway_error")
        return self._settle(call, body)

    async def _again(self, prior: object, call: dict) -> dict:
        if (call["amount"], call["currency"]) != (prior["amount"], prior["currency"]):
            return self._out("denied", call, reason="order_already_has_a_refund_intent")
        if prior["state"] not in ("pending", "unknown"):
            return self._out(
                "replayed",
                call,
                reason=prior["state"],
                approval_id=prior["approval_id"],
                refund_id=prior["refund_id"],
                request_id=prior["request_id"],
            )
        age = datetime.fromisoformat(now()) - datetime.fromisoformat(prior["sent_at"])
        if age.total_seconds() > RESEND_WINDOW:
            return self._out("unknown", call, reason="needs_a_person_to_reconcile")
        try:
            # Exactly the stored request, under the stored id. Policies are not
            # run again: if the first send did land, PayPal's record would now
            # make this look like a refund too many.
            body = await self._paypal.refund(
                prior["capture_id"], prior["amount"], prior["currency"], prior["request_id"]
            )
        except PayPalUnknown:
            self._store.set_state(prior["order_id"], "unknown")
            return self._out("unknown", call, reason="paypal_outcome_unknown")
        except PayPalRejected as refused:
            self._store.set_state(prior["order_id"], "failed")
            return self._out("failed", call, reason=refused.issue, debug_id=refused.debug_id)
        return self._settle(call, body)

    def _settle(self, call: dict, body: dict) -> dict:
        status = short(body.get("status"))
        refund_id = short(body.get("id"))
        done = status in COUNTS_AS_REFUNDED and bool(refund_id)
        self._store.set_state(call["order_id"], "completed" if done else "failed", refund_id)
        return self._out(
            "refunded" if done else "failed",
            call,
            reason="" if done else "paypal_refund_not_completed",
            refund_id=refund_id,
            paypal_status=status,
            request_id=self._store.intent(call["order_id"])["request_id"],
        )

    def _abandon_hold(self, order_id: object) -> None:
        row = self._store.intent(order_id)
        if row is not None and row["state"] == "held":
            self._store.set_state(order_id, "rejected")

    def _out(self, outcome: str, call: dict, actor: str = "agent", **fields: str) -> dict:
        # Only well-formed values reach the audit trail. A malformed argument
        # could be text an attacker wrote, so it is recorded as empty.
        shaped = {
            key: call[key]
            for key in CALL_FIELDS
            if isinstance(call.get(key), str) and re.fullmatch(PATTERNS[key], call[key])
        }
        self._store.record(outcome, actor=actor, **shaped, **fields)
        return {"outcome": outcome, **{k: v for k, v in fields.items() if v}}
