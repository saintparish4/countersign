"""A stand-in for PayPal at the HTTP layer, so the real client code is what gets tested.

FIXTURE, not captured traffic. The shapes follow PayPal's published OpenAPI
documents (Orders v2 `order`, Payments v2 `refund`), and the behaviour is this
file's reading of them: a repeated PayPal-Request-Id returns the first result
with 200, and a refund larger than what is left returns 422
REFUND_AMOUNT_EXCEEDED. Only the sandbox can confirm that reading.

Every request is kept in `requests`, which is what the property tests inspect.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from countersign.gateway import Gateway
from countersign.paypal import HttpPayPal
from countersign.policy import Limits
from countersign.store import Store

SECRET = "sk-canary-CLIENT-SECRET-7f3a"
TOKEN = "A21canaryACCESSTOKEN9d1e"
PAYER_EMAIL = "canary.buyer@personal.example.com"
PAYER_NAME = "Canarina Buyerson"
MERCHANT = "MERCHANT0000A"


class FakePayPal:
    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.requests: list[dict] = []  # every refund request that arrived
        self.applied: dict[str, dict] = {}  # request id -> the refund it created
        self.fault = ""  # "timeout_before", "timeout_after", "http_500" or "order_down"

    def add_order(self, order_id: str, captured: str, refunded: str = "0.00") -> str:
        capture_id = "CAP" + order_id[-14:]
        refunds = []
        if Decimal(refunded) > 0:
            refunds.append(_refund("PRIORREFUND000001", refunded))
        self.orders[order_id] = {
            "id": order_id,
            "status": "COMPLETED",
            "payer": {"email_address": PAYER_EMAIL, "name": {"given_name": PAYER_NAME}},
            "purchase_units": [
                {
                    "payee": {"merchant_id": MERCHANT},
                    "payments": {
                        "captures": [
                            {
                                "id": capture_id,
                                "status": "COMPLETED",
                                "amount": {"currency_code": "USD", "value": captured},
                            }
                        ],
                        "refunds": refunds,
                    },
                }
            ],
        }
        return capture_id

    def refunded_total(self, order_id: str) -> Decimal:
        refunds = self.orders[order_id]["purchase_units"][0]["payments"]["refunds"]
        return sum((Decimal(r["amount"]["value"]) for r in refunds), Decimal("0"))

    def new_refunds(self, order_id: str) -> list[str]:
        """Amounts of the refunds created through this fake for one order."""
        capture = self.orders[order_id]["purchase_units"][0]["payments"]["captures"][0]["id"]
        return [
            r["refund"]["amount"]["value"] for r in self.applied.values() if r["capture"] == capture
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": TOKEN, "expires_in": 3600})
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        if request.method == "GET" and path.startswith("/v2/checkout/orders/"):
            if self.fault == "order_down":
                return httpx.Response(503, json={})
            order = self.orders.get(path.rsplit("/", 1)[1])
            if order is None:
                return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
            return httpx.Response(200, json=order)
        if request.method == "POST" and path.endswith("/refund"):
            return self._refund(request)
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})

    def _refund(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        capture_id = request.url.path.split("/")[-2]
        request_id = request.headers.get("PayPal-Request-Id", "")
        self.requests.append({"capture": capture_id, "request_id": request_id, "body": body})
        fault, self.fault = self.fault, ""
        if fault == "timeout_before":
            raise httpx.ReadTimeout("fake timeout", request=request)
        if fault == "http_500":
            return httpx.Response(500, json={"name": "INTERNAL_SERVER_ERROR"})
        if request_id in self.applied:
            return httpx.Response(200, json=self.applied[request_id]["refund"])
        order = next(
            o
            for o in self.orders.values()
            if o["purchase_units"][0]["payments"]["captures"][0]["id"] == capture_id
        )
        payments = order["purchase_units"][0]["payments"]
        captured = Decimal(payments["captures"][0]["amount"]["value"])
        # An empty body is a full refund, as the Payments v2 description says.
        value = (body.get("amount") or {}).get("value") or str(
            captured - self.refunded_total(order["id"])
        )
        if Decimal(value) > captured - self.refunded_total(order["id"]):
            return httpx.Response(
                422,
                json={
                    "name": "UNPROCESSABLE_ENTITY",
                    "debug_id": "fakedebug0001",
                    "details": [{"issue": "REFUND_AMOUNT_EXCEEDED"}],
                },
            )
        refund = _refund(f"REFUND{len(self.applied):011d}", value)
        payments["refunds"].append(refund)
        payments["captures"][0]["status"] = "PARTIALLY_REFUNDED"
        self.applied[request_id] = {"capture": capture_id, "refund": refund}
        if fault == "timeout_after":
            raise httpx.ReadTimeout("fake timeout", request=request)
        return httpx.Response(201, json=refund)


def _refund(refund_id: str, value: str) -> dict:
    return {
        "id": refund_id,
        "status": "COMPLETED",
        "amount": {"currency_code": "USD", "value": value},
    }


def make_gateway(limits: Limits | None = None) -> tuple[Gateway, FakePayPal, Store]:
    fake = FakePayPal()
    paypal = HttpPayPal("client-id", SECRET, transport=httpx.MockTransport(fake.handler))
    store = Store()
    limits = limits or Limits(merchant_id=MERCHANT)
    return Gateway(paypal, store, limits), fake, store
