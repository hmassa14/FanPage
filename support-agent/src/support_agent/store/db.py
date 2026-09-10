"""SQLite persistence: tickets, per-stage events (audit log), usage, outbox, approvals.

SQLite with WAL is a legitimate production choice for a single-node service at support
volumes (thousands of tickets/day). The repository interface is small enough that moving
to Postgres is a mechanical change.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import InboundEmail, StageUsage, Ticket, TicketStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  status TEXT NOT NULL,
  from_address TEXT NOT NULL,
  subject TEXT NOT NULL,
  category TEXT,
  gate_decision TEXT,
  ticket_json TEXT NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  stage TEXT NOT NULL,
  level TEXT NOT NULL,
  message TEXT NOT NULL,
  data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id);
CREATE TABLE IF NOT EXISTS usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  model TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER,
  cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  latency_ms INTEGER, cost_usd REAL, ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL UNIQUE,
  to_address TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  in_reply_to TEXT,
  status TEXT NOT NULL,          -- pending | sent | failed
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id TEXT NOT NULL,
  reviewer TEXT NOT NULL,
  action TEXT NOT NULL,          -- approve | reject | escalate
  edited INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  ts TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ---- tickets ---------------------------------------------------------- #
    def create_ticket_if_new(self, email: InboundEmail, trace_id: str) -> tuple[Ticket, bool]:
        """Idempotent insert keyed on the email's idempotency key. Returns (ticket, created)."""
        tid = email.idempotency_key()
        with self.tx() as c:
            row = c.execute("SELECT ticket_json FROM tickets WHERE id=?", (tid,)).fetchone()
            if row:
                return Ticket.model_validate_json(row["ticket_json"]), False
            t = Ticket(id=tid, trace_id=trace_id, status=TicketStatus.received, email=email)
            c.execute(
                "INSERT INTO tickets(id,trace_id,status,from_address,subject,ticket_json,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    t.id,
                    trace_id,
                    t.status.value,
                    email.from_address,
                    email.subject,
                    t.model_dump_json(),
                    _now(),
                    _now(),
                ),
            )
        return t, True

    def claim(self, ticket_id: str) -> bool:
        """Atomically move received -> processing so two workers never process one ticket."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE tickets SET status=?, updated_at=? WHERE id=? AND status=?",
                (TicketStatus.processing.value, _now(), ticket_id, TicketStatus.received.value),
            )
            return cur.rowcount == 1

    def save(self, t: Ticket) -> None:
        t.updated_at = datetime.now(UTC)
        with self.tx() as c:
            c.execute(
                "UPDATE tickets SET status=?, category=?, gate_decision=?, ticket_json=?, "
                "error=?, updated_at=? WHERE id=?",
                (
                    t.status.value,
                    t.triage.category.value if t.triage else None,
                    t.gate.decision.value if t.gate else None,
                    t.model_dump_json(),
                    t.error,
                    t.updated_at.isoformat(),
                    t.id,
                ),
            )

    def get(self, ticket_id: str) -> Ticket | None:
        row = self._conn.execute("SELECT ticket_json FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        return Ticket.model_validate_json(row["ticket_json"]) if row else None

    def list_tickets(self, status: TicketStatus | None = None, limit: int = 100) -> list[Ticket]:
        q = "SELECT ticket_json FROM tickets"
        args: tuple[Any, ...] = ()
        if status:
            q += " WHERE status=?"
            args = (status.value,)
        q += " ORDER BY created_at DESC LIMIT ?"
        rows = self._conn.execute(q, (*args, limit)).fetchall()
        return [Ticket.model_validate_json(r["ticket_json"]) for r in rows]

    def counts_by_status(self) -> dict[str, int]:
        rows = self._conn.execute("SELECT status, COUNT(*) n FROM tickets GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ---- audit events ------------------------------------------------------ #
    def event(self, ticket_id: str, stage: str, message: str, level: str = "info", **data: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(ticket_id,ts,stage,level,message,data_json) VALUES (?,?,?,?,?,?)",
                (
                    ticket_id,
                    _now(),
                    stage,
                    level,
                    message,
                    json.dumps(data, default=str) if data else None,
                ),
            )

    def events(self, ticket_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts,stage,level,message,data_json FROM events WHERE ticket_id=? ORDER BY id",
            (ticket_id,),
        ).fetchall()
        return [{**dict(r), "data": json.loads(r["data_json"]) if r["data_json"] else None} for r in rows]

    def record_usage(self, ticket_id: str, u: StageUsage) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO usage(ticket_id,stage,model,input_tokens,output_tokens,cache_read_tokens,"
                "cache_write_tokens,latency_ms,cost_usd,ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    ticket_id,
                    u.stage,
                    u.model,
                    u.input_tokens,
                    u.output_tokens,
                    u.cache_read_tokens,
                    u.cache_write_tokens,
                    u.latency_ms,
                    u.cost_usd,
                    _now(),
                ),
            )

    def usage_totals(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o,"
            " COALESCE(SUM(cache_read_tokens),0) cr, COALESCE(SUM(cost_usd),0) cost FROM usage"
        ).fetchone()
        return {
            "llm_calls": row["calls"],
            "input_tokens": row["i"],
            "output_tokens": row["o"],
            "cache_read_tokens": row["cr"],
            "cost_usd": round(row["cost"], 6),
        }

    # ---- outbox ------------------------------------------------------------- #
    def enqueue_outbox(
        self, ticket_id: str, to_address: str, subject: str, body: str, in_reply_to: str | None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO outbox(ticket_id,to_address,subject,body,in_reply_to,status,"
                "created_at) VALUES (?,?,?,?,?,'pending',?)",
                (ticket_id, to_address, subject, body, in_reply_to, _now()),
            )

    def pending_outbox(self, max_attempts: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM outbox WHERE status='pending' AND attempts<? ORDER BY id LIMIT ?",
            (max_attempts, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_outbox(self, outbox_id: int, *, sent: bool, error: str | None = None) -> None:
        with self.tx() as c:
            if sent:
                c.execute(
                    "UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?",
                    (_now(), outbox_id),
                )
            else:
                c.execute(
                    "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                    (error, outbox_id),
                )

    def fail_exhausted_outbox(self, max_attempts: int) -> int:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE outbox SET status='failed' WHERE status='pending' AND attempts>=?",
                (max_attempts,),
            )
            return cur.rowcount

    def outbox_for(self, ticket_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM outbox WHERE ticket_id=?", (ticket_id,)).fetchone()
        return dict(row) if row else None

    # ---- approvals ---------------------------------------------------------- #
    def record_approval(
        self, ticket_id: str, reviewer: str, action: str, edited: bool, note: str | None
    ) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO approvals(ticket_id,reviewer,action,edited,note,ts) VALUES (?,?,?,?,?,?)",
                (ticket_id, reviewer, action, int(edited), note, _now()),
            )

    def close(self) -> None:
        self._conn.close()
