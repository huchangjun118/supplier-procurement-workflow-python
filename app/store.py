"""SQLite-backed store. Every mutation runs in a single transaction so a
failed step never leaves partial writes; committed rows survive restart."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from .errors import Reason, WorkflowError

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requisitions (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, amount REAL NOT NULL,
    status TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY, requisition_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
    amount REAL NOT NULL, valid_until TEXT NOT NULL, status TEXT NOT NULL,
    seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY, requisition_id TEXT NOT NULL, step INTEGER NOT NULL,
    role TEXT NOT NULL, assignee TEXT NOT NULL, status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id TEXT PRIMARY KEY, requisition_id TEXT NOT NULL, supplier_id TEXT NOT NULL,
    amount REAL NOT NULL, status TEXT NOT NULL, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS po_versions (
    po_id TEXT NOT NULL, version INTEGER NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (po_id, version));
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, response TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT NOT NULL,
    entity_id TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
"""


class Store:
    """Thread-safe SQLite store; one connection guarded by an RLock.

    `transaction()` uses BEGIN IMMEDIATE so concurrent writers serialize:
    the first committed result wins and conflicting writes see final state.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self):
        """Atomic unit of work: rollback on any exception (no partial writes)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    # -- generic helpers -------------------------------------------------
    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # -- idempotency ------------------------------------------------------
    def idempotent_get(self, conn, key: str, request_hash: str):
        """Return the stored response for `key`, or None if unseen.

        Same key with a different payload is a caller bug, not a retry.
        """
        row = conn.execute(
            "SELECT request_hash, response FROM idempotency_keys WHERE key=?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise WorkflowError(
                Reason.IDEMPOTENCY_CONFLICT,
                f"idempotency key {key!r} reused with a different payload",
            )
        return json.loads(row["response"])

    def idempotent_put(self, conn, key: str, request_hash: str, response) -> None:
        conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash, response) VALUES (?,?,?)",
            (key, request_hash, json.dumps(response)),
        )

    # -- audit trail -------------------------------------------------------
    def emit(self, conn, entity: str, entity_id: str, action: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO events (entity, entity_id, action, detail) VALUES (?,?,?,?)",
            (entity, entity_id, action, json.dumps(detail, ensure_ascii=False)),
        )

    def events_for(self, entity: str, entity_id: str) -> list[dict]:
        rows = self.all(
            "SELECT action, detail FROM events WHERE entity=? AND entity_id=? ORDER BY id",
            (entity, entity_id),
        )
        return [{"action": r["action"], "detail": json.loads(r["detail"])} for r in rows]
