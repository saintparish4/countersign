"""The gateway's HTTP surface, and the two credentials that divide it.

The agent token reaches two routes: ask for a refund, and read the facts of
its own session. The operator token reaches the rest: open a session for an
order, see what is waiting, decide it, read the audit trail. The agent process
is never given the operator token or the PayPal credentials, so it cannot open
a session for an order it likes, approve its own request, or call PayPal.

The refund body goes to the gateway unparsed. The gateway is the one place
that judges it, so a malformed request is refused and audited by the same code
as every other request.
"""

from __future__ import annotations

import hmac
import os
from decimal import Decimal

from fastapi import Body, Depends, FastAPI, Header, HTTPException

from countersign.gateway import Gateway
from countersign.paypal import SANDBOX, HttpPayPal
from countersign.policy import Limits
from countersign.store import Store


def create_app(gateway: Gateway, agent_token: str, operator_token: str) -> FastAPI:
    if min(len(agent_token), len(operator_token)) < 24 or agent_token == operator_token:
        raise ValueError("the agent and operator tokens must differ and be 24+ characters")

    def bearer(expected: str):
        def check(authorization: str = Header(default="")) -> None:
            if not hmac.compare_digest(authorization.encode(), f"Bearer {expected}".encode()):
                raise HTTPException(status_code=401, detail="unauthorized")

        return Depends(check)

    agent, operator = bearer(agent_token), bearer(operator_token)
    app = FastAPI(title="countersign", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.post("/v1/refunds", dependencies=[agent])
    async def refund(call: object = Body(default=None)) -> dict:
        return await gateway.refund(call)

    @app.get("/v1/sessions/{session_id}", dependencies=[agent])
    async def session_facts(session_id: str) -> dict:
        try:
            return await gateway.session_facts(session_id)
        except Exception:
            raise HTTPException(status_code=404, detail="no facts for this session") from None

    @app.post("/v1/sessions", dependencies=[operator])
    async def open_session(body: dict = Body()) -> dict:
        try:
            return {"session_id": gateway.open_session(str(body.get("order_id", "")))}
        except ValueError:
            raise HTTPException(status_code=422, detail="not an order id") from None

    @app.get("/v1/approvals", dependencies=[operator])
    async def approvals() -> list[dict]:
        return gateway.pending()

    @app.post("/v1/approvals/{approval_id}", dependencies=[operator])
    async def decide(approval_id: str, body: dict = Body()) -> dict:
        return await gateway.decide(approval_id, approve=body.get("approve") is True)

    @app.get("/v1/audit", dependencies=[operator])
    async def audit() -> list[dict]:
        return gateway.audit()

    return app


def from_env() -> FastAPI:
    """`uvicorn countersign.app:from_env --factory`. A missing setting stops the start."""
    env = os.environ
    limits = Limits(
        currency=env.get("COUNTERSIGN_CURRENCY", "USD"),
        auto_max=Decimal(env.get("COUNTERSIGN_AUTO_MAX", "25.00")),
        hard_max=Decimal(env.get("COUNTERSIGN_HARD_MAX", "200.00")),
        daily_auto_cap=Decimal(env.get("COUNTERSIGN_DAILY_AUTO_CAP", "100.00")),
        merchant_id=env["PAYPAL_MERCHANT_ID"],
    )
    paypal = HttpPayPal(
        env["PAYPAL_CLIENT_ID"], env["PAYPAL_CLIENT_SECRET"], env.get("PAYPAL_BASE_URL", SANDBOX)
    )
    gateway = Gateway(paypal, Store(env.get("COUNTERSIGN_DB", "countersign.db")), limits)
    return create_app(gateway, env["COUNTERSIGN_AGENT_TOKEN"], env["COUNTERSIGN_OPERATOR_TOKEN"])
