"""The gateway's memory: which order a session may touch, one refund intent per
order, and an append-only audit trail. SQLite, written synchronously.

OpenScript's own AuditPolicy is not used for this. It queues events, drops the
oldest when full and fails open, which suits observability and does not suit a
record that has to reconcile one-to-one with PayPal.

The audit table has no free-text column. A customer message, a payer's name or
email, a token or a header has nowhere to be written. `reason` only ever holds
one of the short codes in this package.

An intent row is written and committed before PayPal is called. A crash after
that leaves a row in state 'pending', and the retry reuses its request id.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, order_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    order_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, amount TEXT NOT NULL,
    currency TEXT NOT NULL, state TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL DEFAULT '', capture_id TEXT NOT NULL DEFAULT '',
    captured TEXT NOT NULL DEFAULT '', refunded TEXT NOT NULL DEFAULT '',
    refund_id TEXT NOT NULL DEFAULT '', by_human INTEGER NOT NULL DEFAULT 0,
    sent_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, event TEXT NOT NULL,
    actor TEXT NOT NULL, session_id TEXT NOT NULL, order_id TEXT NOT NULL,
    amount TEXT NOT NULL, currency TEXT NOT NULL, reason TEXT NOT NULL,
    approval_id TEXT NOT NULL, request_id TEXT NOT NULL, refund_id TEXT NOT NULL,
    paypal_status TEXT NOT NULL, debug_id TEXT NOT NULL);
"""
AUDIT_FIELDS = (
    "session_id", "order_id", "amount", "currency", "reason", "approval_id",
    "request_id", "refund_id", "paypal_status", "debug_id",
)  # fmt: skip


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def short(value: object, limit: int = 64) -> str:
    """Only text is kept, and only so much of it."""
    return value[:limit] if isinstance(value, str) else ""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        # Every route is `async def`, so all access happens on the one event-loop
        # thread; the check is off only because that thread is not the one that
        # built the app.
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)

    def open_session(self, order_id: str) -> str:
        session_id = uuid.uuid4().hex
        self._db.execute("INSERT INTO sessions VALUES (?, ?)", (session_id, order_id))
        return session_id

    def session_order(self, session_id: object) -> str | None:
        if not isinstance(session_id, str):
            return None
        row = self._db.execute(
            "SELECT order_id FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row["order_id"] if row else None

    def intent(self, order_id: object) -> sqlite3.Row | None:
        if not isinstance(order_id, str):
            return None
        return self._db.execute("SELECT * FROM intents WHERE order_id = ?", (order_id,)).fetchone()

    def intent_for_approval(self, approval_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM intents WHERE approval_id = ? AND state = 'held'", (approval_id,)
        ).fetchone()

    def held(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM intents WHERE state = 'held'").fetchall()

    def hold(self, call: dict, approval_id: str, captured: str, refunded: str) -> None:
        self._db.execute(
            "INSERT INTO intents (order_id, session_id, amount, currency, state, request_id,"
            " approval_id, captured, refunded) VALUES (?, ?, ?, ?, 'held', ?, ?, ?, ?)",
            (call["order_id"], call["session_id"], call["amount"], call["currency"],
             str(uuid.uuid4()), approval_id, captured, refunded),
        )  # fmt: skip

    def begin(self, call: dict, capture_id: str) -> str:
        """Write the intent down as 'pending' before PayPal is called; return its request id."""
        row = self.intent(call["order_id"])
        if row is None:
            self._db.execute(
                "INSERT INTO intents (order_id, session_id, amount, currency, state, request_id)"
                " VALUES (?, ?, ?, ?, 'pending', ?)",
                (call["order_id"], call["session_id"], call["amount"], call["currency"],
                 str(uuid.uuid4())),
            )  # fmt: skip
        elif (row["state"], row["amount"], row["currency"]) != (
            "held", call["amount"], call["currency"],
        ):  # fmt: skip
            raise RuntimeError("this order already has a different refund intent")
        self._db.execute(
            "UPDATE intents SET state = 'pending', capture_id = ?, sent_at = ?,"
            " by_human = (approval_id != '') WHERE order_id = ?",
            (capture_id, now(), call["order_id"]),
        )
        return self.intent(call["order_id"])["request_id"]

    def set_state(self, order_id: str, state: str, refund_id: str = "") -> None:
        self._db.execute(
            "UPDATE intents SET state = ?, refund_id = ? WHERE order_id = ?",
            (state, refund_id, order_id),
        )

    def auto_refunds_today(self) -> list[str]:
        """Amounts sent today without a person, including any whose outcome is unknown."""
        rows = self._db.execute(
            "SELECT amount FROM intents WHERE by_human = 0 AND substr(sent_at, 1, 10) = ?"
            " AND state IN ('pending', 'unknown', 'completed')",
            (now()[:10],),
        ).fetchall()
        return [row["amount"] for row in rows]

    def record(self, event: str, actor: str = "agent", **fields: object) -> None:
        values = [short(fields.get(name)) for name in AUDIT_FIELDS]
        self._db.execute(
            f"INSERT INTO audit (at, event, actor, {', '.join(AUDIT_FIELDS)})"
            f" VALUES (?, ?, ?{', ?' * len(AUDIT_FIELDS)})",
            (now(), event, actor, *values),
        )

    def audit(self) -> list[dict]:
        return [dict(row) for row in self._db.execute("SELECT * FROM audit ORDER BY seq")]
