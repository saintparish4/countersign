"""Hypothesis searches for any sequence of calls that makes PayPal see a refund
outside policy. The agent is assumed fully compromised: it may send anything.

One session exists and it is bound to MINE. THEIRS is another customer's order
with more money on it. Faults are injected at random between calls. The
assertions read only what arrived at the fake PayPal.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from hypothesis import event, given, settings
from hypothesis import strategies as st

from countersign.policy import PATTERNS, Limits
from tests.fakes import MERCHANT, make_gateway

MINE = "ORDER00000000000A"
THEIRS = "ORDER00000000000B"
LIMITS = Limits(merchant_id=MERCHANT)

junk = st.one_of(
    st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=40),
    st.lists(st.text(max_size=5), max_size=2), st.dictionaries(st.text(max_size=3), st.integers()),
    st.sampled_from(["-5.00", "1e9", "NaN", "Infinity", "12", "12.000", " 12.00", "12.00\n",
                     "١٢.٠٠", "0x10", "USD", "usd", "EUR", THEIRS, "../" + THEIRS]),
)  # fmt: skip
faults = st.sampled_from(["", "", "", "timeout_before", "timeout_after", "http_500", "order_down"])


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


@st.composite
def calls(draw, session: str, left: int):
    """One call from a compromised agent. `left` is what the order still has, in cents.

    The kind of call is chosen first and the values built to fit it. Drawing
    each field independently produced calls that were nearly all refused for
    their shape, so the search passed without ever reaching PayPal.
    """
    kind = draw(
        st.sampled_from(
            ["small", "small", "small", "needs_person", "needs_person", "too_much", "any_amount",
             "other_order", "forged_session", "wrong_currency", "junk_field", "extra", "missing"]
        )
    )  # fmt: skip
    amount = {
        "needs_person": st.integers(2_501, 20_000),
        "too_much": st.integers(left + 1, left + 50_000),
        "any_amount": st.integers(0, 60_000),
    }.get(kind, st.integers(1, 2_500))
    call = {
        "session_id": session,
        "order_id": MINE,
        "amount": money(draw(amount)),
        "currency": "USD",
    }
    if kind == "other_order":
        call["order_id"] = THEIRS
    elif kind == "forged_session":
        call["session_id"] = draw(st.sampled_from(["0" * 32, "f" * 32, session[::-1]]))
    elif kind == "wrong_currency":
        call["currency"] = "EUR"
    elif kind == "junk_field":
        call[draw(st.sampled_from(sorted(call)))] = draw(junk)
    elif kind == "extra":
        name = draw(st.sampled_from(["capture_id", "note_to_payer", "approval_id", "x"]))
        call[name] = draw(junk)
    elif kind == "missing":
        del call[draw(st.sampled_from(sorted(call)))]
    return call


@settings(max_examples=400, deadline=None)
@given(data=st.data(), captured=st.integers(1, 30_000), refunded_share=st.integers(0, 100))
def test_nothing_outside_policy_is_ever_sent(data, captured, refunded_share):
    already = captured * refunded_share // 100

    async def run():
        gateway, fake, _ = make_gateway(LIMITS)
        capture = fake.add_order(MINE, money(captured), money(already))
        fake.add_order(THEIRS, "500.00")
        session = gateway.open_session(MINE)
        approved = False
        for _ in range(data.draw(st.integers(1, 6))):
            fake.fault = data.draw(faults)
            await gateway.refund(data.draw(calls(session, captured - already)))
            if data.draw(st.booleans()):
                for waiting in gateway.pending():
                    approved = True
                    await gateway.decide(waiting["approval_id"], approve=True)
        return fake, capture, approved

    fake, capture, approved = asyncio.run(run())
    event(f"refund requests reaching PayPal: {min(len(fake.requests), 3)}")
    event(f"a person approved: {approved}")
    ceiling = LIMITS.hard_max if approved else LIMITS.auto_max
    left_before = Decimal(captured - already) / 100

    for sent in fake.requests:
        assert sent["capture"] == capture  # never another customer's payment
        assert set(sent["body"]) == {"amount", "custom_id"}  # never an empty "refund it all"
        value = sent["body"]["amount"]["value"]
        assert re.fullmatch(PATTERNS["amount"], value)
        assert Decimal("0") < Decimal(value) <= min(ceiling, left_before)
        assert sent["body"]["amount"]["currency_code"] == "USD"
        assert sent["request_id"] and sent["body"]["custom_id"] == sent["request_id"]
    assert len({sent["request_id"] for sent in fake.requests}) <= 1  # one intent per order
    assert len(fake.applied) <= 1  # so at most one refund exists
    assert fake.refunded_total(MINE) <= Decimal(captured) / 100
    assert fake.refunded_total(THEIRS) == 0


@settings(max_examples=100, deadline=None)
@given(amounts=st.lists(st.integers(1, 2_500), min_size=1, max_size=12))
def test_refunds_without_a_person_never_exceed_the_daily_cap(amounts):
    async def run():
        gateway, fake, _ = make_gateway(LIMITS)
        for index, amount in enumerate(amounts):
            order = f"ORDER{index:012d}"
            fake.add_order(order, "100.00")
            await gateway.refund(
                {
                    "session_id": gateway.open_session(order),
                    "order_id": order,
                    "amount": money(amount),
                    "currency": "USD",
                }
            )
        return fake

    fake = asyncio.run(run())
    total = sum(Decimal(e["refund"]["amount"]["value"]) for e in fake.applied.values())
    assert total <= LIMITS.daily_auto_cap
