"""The only code that holds PayPal credentials and the only code that calls PayPal.

Two calls exist: read an order, refund a capture. The refund body is always
an explicit amount plus our own id. PayPal reads an empty body as "refund
everything" (Payments v2 spec, captures.refund), so an amount is never
optional here. Nothing the customer or the model wrote is sent to PayPal.

Failures are split by what they tell us about money. PayPalRejected means
PayPal answered and refused. PayPalUnknown means we cannot tell whether the
refund happened (timeout, 5xx, 409 in progress), and the caller must retry
with the same PayPal-Request-Id and never with a new one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
COUNTS_AS_REFUNDED =("COMPLETED", "REFUNDED")


class PayPalRejected(Exception):
    def __init__(self, status: int, issue: str, debug_id: str) -> None:
        super().__init__(f"PayPal refused: {status} {issue}")
        self.status, self.issue, self.debug_id = status, issue, debug_id


class PayPalUnknown(Exception):
    """The outcome of the call is not known"""


@dataclass(frozen=True)
class OrderFacts:
    """What PayPal's own record says about one order. Every limit starts here"""

    order_id: str
    status: str
    merchant_id: str
    capture_id: str
    capture_status: str
    currency: str
    captured: Decimal
    refunded: Decimal

    @property
    def refundable(self) -> Decimal:
        return self.captured - self.refunded


def facts_from_order(order: dict) -> OrderFacts:
    """Read an Orders v2 record. Anything but one unit with one capture is refused"""
    units = order.get("purchase_units") or []
    if len(units) != 1:
        raise ValueError("order does not have exactly one purchase unit")
    payments = units[0].get("payments") or {}
    captures = payments.get("captures") or []
    if len(captures) != 1:
        raise ValueError("order does not have exactly one capture")
    capture = captures[0]
    currency = capture["amount"]["currency_code"]
    refunded = Decimal("0")
    for refund in payments.get("refunds") or []:
        if refund.get("status") in COUNTS_AS_REFUNDED:
            if refund["amount"]["currency_code"] != currency:
                raise ValueError("a refund is in another currency than the capture")
            refunded += Decimal(refund["amount"]["value"])
    return OrderFacts(
        order_id=order["id"],
        status=order["status"],
        merchant_id=(units[0].get("payee") or {}).get("merchant_id", ""),
        capture_id=capture["id"],
        capture_status=capture["status"],
        currency=currency,
        captured=Decimal(capture["amount"]["value"]),
        refunded=refunded,
    )


class HttpPayPal:
    def __init__(
        self,
        client_id: str,
        secret: str,
        base_url: str = SANDBOX,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._auth = (client_id, secret)
        self._http = httpx.AsyncClient(
            base_url=base_url, transport=transport, timeout=httpx.Timeout(20.0, connect=5.0)
        )
        self._token = ""
        self._token_expires = 0.0

    async def _bearer(self) -> dict[str, str]:
        if time.monotonic() >= self._token_expires:
            reply = await self._http.post(
                "/v1/oauth2/token", data={"grant_type": "client_credentials"}, auth=self._auth
            )
            reply.raise_for_status()
            body = reply.json()
            self._token = body["access_token"]
            self._token_expires = time.monotonic() + int(body.get("expires_in", 300)) - 60
        return {"Authorization": f"Bearer {self._token}"}

    async def _call(self, method: str, path: str, **kwargs: object) -> dict:
        try:
            headers = {**await self._bearer(), **kwargs.pop("headers", {})}
            reply = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # The type only. An httpx error message can carry the request URL.
            raise PayPalUnknown(type(exc).__name__) from None
        if reply.status_code in (200, 201):
            return reply.json()
        if reply.status_code == 409 or reply.status_code >= 500:
            raise PayPalUnknown(f"http {reply.status_code}")
        try:
            body = reply.json()
        except ValueError:
            body = {}
        details = body.get("details") or [{}]
        issue = details[0].get("issue") or body.get("name") or "UNKNOWN"
        raise PayPalRejected(reply.status_code, str(issue), str(body.get("debug_id", "")))

    async def get_order(self, order_id: str) -> dict:
        return await self._call("GET", f"/v2/checkout/orders/{order_id}")

    async def refund(self, capture_id: str, amount: str, currency: str, request_id: str) -> dict:
        return await self._call(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            headers={"PayPal-Request-Id": request_id, "Prefer": "return=representation"},
            json={"amount": {"value": amount, "currency_code": currency}, "custom_id": request_id},
        )
