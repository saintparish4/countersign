"""A gateway wired to the FAKE PayPal from tests/fakes.py, for trying the product
with no PayPal account. Nothing here touches PayPal, and nothing it prints is
evidence about PayPal.

    uvicorn evals.offline:app --factory --port 8787

Two orders exist: ORDER00000000000A (60.00 USD captured) and ORDER00000000000B
(500.00 USD captured, another customer's).
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from countersign.app import create_app
from tests.fakes import make_gateway


def app() -> FastAPI:
    gateway, fake, _ = make_gateway()
    fake.add_order("ORDER00000000000A", "60.00")
    fake.add_order("ORDER00000000000B", "500.00")
    return create_app(
        gateway, os.environ["COUNTERSIGN_AGENT_TOKEN"], os.environ["COUNTERSIGN_OPERATOR_TOKEN"]
    )
