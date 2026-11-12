"""The same guarantees through HTTP, plus who may call what."""

from __future__ import annotations

from fastapi.testclient import TestClient

from countersign.app import create_app
from tests.fakes import PAYER_EMAIL, SECRET, TOKEN, make_gateway

AGENT = {"Authorization": "Bearer agent-token-0123456789abcdef"}
OPERATOR = {"Authorization": "Bearer operator-token-0123456789abcd"}
MINE = "ORDER00000000000A"
THEIRS = "ORDER00000000000B"


def client():
    gateway, fake, _ = make_gateway()
    fake.add_order(MINE, "60.00")
    fake.add_order(THEIRS, "500.00")
    app = create_app(gateway, AGENT["Authorization"][7:], OPERATOR["Authorization"][7:])
    return TestClient(app), fake


def test_the_agent_token_cannot_reach_operator_routes_and_no_token_reaches_nothing():
    with client()[0] as http:
        for method, path in [
            ("POST", "/v1/sessions"),
            ("GET", "/v1/approvals"),
            ("POST", "/v1/approvals/abc"),
            ("GET", "/v1/audit"),
        ]:
            assert http.request(method, path, headers=AGENT, json={}).status_code == 401
        for method, path in [("POST", "/v1/refunds"), ("GET", "/v1/sessions/abc")]:
            assert http.request(method, path, json={}).status_code == 401
            assert http.request(method, path, headers=OPERATOR, json={}).status_code == 401


def test_one_ticket_end_to_end_over_http():
    http, fake = client()
    with http:
        opened = http.post("/v1/sessions", headers=OPERATOR, json={"order_id": MINE})
        session = opened.json()["session_id"]
        facts = http.get(f"/v1/sessions/{session}", headers=AGENT).json()
        assert facts["refundable"] == "60.00" and PAYER_EMAIL not in repr(facts)

        ask = {"session_id": session, "order_id": MINE, "amount": "60.00", "currency": "USD"}
        stolen = http.post("/v1/refunds", headers=AGENT, json={**ask, "order_id": THEIRS})
        assert stolen.json()["outcome"] == "denied"
        extra = http.post("/v1/refunds", headers=AGENT, json={**ask, "capture_id": "CAPX"})
        assert extra.json() == {"outcome": "denied", "reason": "malformed"}
        not_json = http.post("/v1/refunds", headers=AGENT, content=b"refund it all")
        assert not_json.json() == {"outcome": "denied", "reason": "malformed"}

        held = http.post("/v1/refunds", headers=AGENT, json=ask).json()
        assert held["outcome"] == "held" and fake.requests == []
        waiting = http.get("/v1/approvals", headers=OPERATOR).json()
        assert [w["amount"] for w in waiting] == ["60.00"]
        self_approval = http.post(
            f"/v1/approvals/{held['approval_id']}", headers=AGENT, json={"approve": True}
        )
        assert self_approval.status_code == 401 and fake.requests == []

        done = http.post(
            f"/v1/approvals/{held['approval_id']}", headers=OPERATOR, json={"approve": True}
        ).json()
        assert done["outcome"] == "refunded" and fake.new_refunds(MINE) == ["60.00"]

        trail = http.get("/v1/audit", headers=OPERATOR).json()
        assert [row["event"] for row in trail] == [
            "denied", "denied", "denied", "held", "approved", "sent", "refunded",
        ]  # fmt: skip
        for canary in (SECRET, TOKEN, PAYER_EMAIL):
            assert canary not in repr(trail)
