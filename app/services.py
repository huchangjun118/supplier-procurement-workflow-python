"""Workflow services: suppliers, requisitions, quotes, approvals, orders.

Every public method is one atomic transaction; an optional idempotency key
makes retries return the first committed result instead of re-executing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field

from .errors import Reason, WorkflowError
from .models import (
    ApprovalStatus,
    POStatus,
    QuoteStatus,
    RequisitionStatus,
    Role,
    SupplierStatus,
)
from .state_machine import (
    PO_TRANSITIONS,
    REQUISITION_TRANSITIONS,
    assert_transition,
)
from .store import Store


def _new_id() -> str:
    return uuid.uuid4().hex


def _run_idempotent(store: Store, key: str | None, payload: dict, fn):
    """Execute `fn(conn)` once for `key`; retries replay the stored result."""
    key = key or _new_id()
    request_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    with store.transaction() as conn:
        cached = store.idempotent_get(conn, key, request_hash)
        if cached is not None:
            return cached
        result = fn(conn)
        store.idempotent_put(conn, key, request_hash, result)
        return result


# --------------------------------------------------------------------------
# Suppliers
# --------------------------------------------------------------------------
class SupplierService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def register(self, name: str, supplier_id: str | None = None,
                 key: str | None = None) -> dict:
        sid = supplier_id or _new_id()

        def fn(conn):
            conn.execute(
                "INSERT INTO suppliers (id, name, status) VALUES (?,?,?)",
                (sid, name, SupplierStatus.PENDING.value),
            )
            self._store.emit(conn, "supplier", sid, "registered", {"name": name})
            return {"id": sid, "name": name, "status": SupplierStatus.PENDING.value}

        return _run_idempotent(self._store, key, {"name": name, "id": sid}, fn)

    def get(self, supplier_id: str) -> dict:
        row = self._store.one("SELECT * FROM suppliers WHERE id=?", (supplier_id,))
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND, f"supplier {supplier_id} not found")
        return dict(row)

    def _set_status(self, supplier_id: str, target: SupplierStatus,
                    allowed: set[SupplierStatus], key: str | None) -> dict:
        def fn(conn):
            row = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            if row is None:
                raise WorkflowError(Reason.NOT_FOUND, f"supplier {supplier_id} not found")
            current = SupplierStatus(row["status"])
            if current == target:
                raise WorkflowError(Reason.DUPLICATE_SUBMISSION,
                                    f"supplier already {target.value}")
            if current not in allowed:
                raise WorkflowError(Reason.ILLEGAL_TRANSITION,
                                    f"supplier {current.value} -> {target.value} not allowed")
            conn.execute("UPDATE suppliers SET status=? WHERE id=?",
                         (target.value, supplier_id))
            self._store.emit(conn, "supplier", supplier_id, "status_changed",
                             {"from": current.value, "to": target.value})
            return {"id": supplier_id, "status": target.value}

        return _run_idempotent(self._store, key,
                               {"supplier": supplier_id, "target": target.value}, fn)

    def admit(self, supplier_id: str, key: str | None = None) -> dict:
        return self._set_status(supplier_id, SupplierStatus.ACTIVE,
                                {SupplierStatus.PENDING}, key)

    def suspend(self, supplier_id: str, key: str | None = None) -> dict:
        return self._set_status(supplier_id, SupplierStatus.SUSPENDED,
                                {SupplierStatus.ACTIVE}, key)

    def reinstate(self, supplier_id: str, key: str | None = None) -> dict:
        return self._set_status(supplier_id, SupplierStatus.ACTIVE,
                                {SupplierStatus.SUSPENDED}, key)

    def require_quotable(self, conn, supplier_id: str) -> None:
        """Gate used by quotes and purchase orders (new business only)."""
        row = conn.execute("SELECT status FROM suppliers WHERE id=?",
                           (supplier_id,)).fetchone()
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND, f"supplier {supplier_id} not found")
        status = SupplierStatus(row["status"])
        if status is SupplierStatus.PENDING:
            raise WorkflowError(Reason.SUPPLIER_NOT_ADMITTED,
                                f"supplier {supplier_id} has not passed admission")
        if status is SupplierStatus.SUSPENDED:
            raise WorkflowError(Reason.SUPPLIER_SUSPENDED,
                                f"supplier {supplier_id} is suspended")


# --------------------------------------------------------------------------
# Requisitions
# --------------------------------------------------------------------------
class RequisitionService:
    def __init__(self, store: Store) -> None:
        self._store = store

    def create(self, title: str, amount: float, requisition_id: str | None = None,
               key: str | None = None) -> dict:
        rid = requisition_id or _new_id()

        def fn(conn):
            conn.execute(
                "INSERT INTO requisitions (id, title, amount, status, version)"
                " VALUES (?,?,?,?,0)",
                (rid, title, amount, RequisitionStatus.DRAFT.value),
            )
            self._store.emit(conn, "requisition", rid, "created",
                             {"title": title, "amount": amount})
            return {"id": rid, "status": RequisitionStatus.DRAFT.value}

        return _run_idempotent(self._store, key,
                               {"title": title, "amount": amount, "id": rid}, fn)

    def get(self, requisition_id: str) -> dict:
        row = self._store.one("SELECT * FROM requisitions WHERE id=?", (requisition_id,))
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND,
                                f"requisition {requisition_id} not found")
        return dict(row)

    def _row(self, conn, requisition_id: str):
        row = conn.execute("SELECT * FROM requisitions WHERE id=?",
                           (requisition_id,)).fetchone()
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND,
                                f"requisition {requisition_id} not found")
        return row

    def advance(self, requisition_id: str, target: RequisitionStatus,
                key: str | None = None) -> dict:
        """Generic ordered transition; illegal jumps are rejected with a reason."""
        def fn(conn):
            row = self._row(conn, requisition_id)
            current = RequisitionStatus(row["status"])
            assert_transition(REQUISITION_TRANSITIONS, current, target,
                              "requisition", requisition_id)
            conn.execute(
                "UPDATE requisitions SET status=?, version=version+1 WHERE id=?",
                (target.value, requisition_id))
            self._store.emit(conn, "requisition", requisition_id, "transition",
                             {"from": current.value, "to": target.value})
            return {"id": requisition_id, "status": target.value}

        return _run_idempotent(self._store, key,
                               {"req": requisition_id, "target": target.value}, fn)

    def _force_transition(self, conn, requisition_id: str, current: RequisitionStatus,
                          target: RequisitionStatus, action: str, detail: dict) -> None:
        """Internal transition driven by another service (same transaction)."""
        assert_transition(REQUISITION_TRANSITIONS, current, target,
                          "requisition", requisition_id)
        conn.execute("UPDATE requisitions SET status=?, version=version+1 WHERE id=?",
                     (target.value, requisition_id))
        self._store.emit(conn, "requisition", requisition_id, action, detail)


# --------------------------------------------------------------------------
# Quotes
# --------------------------------------------------------------------------
class QuoteService:
    ADJUDICATION_RULE = "lowest valid amount; ties broken by earliest arrival"

    def __init__(self, store: Store, suppliers: SupplierService,
                 requisitions: RequisitionService) -> None:
        self._store = store
        self._suppliers = suppliers
        self._requisitions = requisitions

    def submit(self, requisition_id: str, supplier_id: str, amount: float,
               valid_until: str, quote_id: str | None = None,
               key: str | None = None) -> dict:
        qid = quote_id or _new_id()
        payload = {"req": requisition_id, "supplier": supplier_id,
                   "amount": amount, "valid_until": valid_until, "id": qid}

        def fn(conn):
            req = self._requisitions._row(conn, requisition_id)
            status = RequisitionStatus(req["status"])
            if status in (RequisitionStatus.DRAFT, RequisitionStatus.SUBMITTED):
                raise WorkflowError(Reason.PRECONDITION_MISSING,
                                    "quoting has not been opened yet")
            if status is not RequisitionStatus.QUOTING:
                # Late arrival must never shift a decided result.
                raise WorkflowError(Reason.QUOTE_WINDOW_CLOSED,
                                    f"quoting window closed (status={status.value})")
            self._suppliers.require_quotable(conn, supplier_id)
            dup = conn.execute(
                "SELECT id FROM quotes WHERE requisition_id=? AND supplier_id=?",
                (requisition_id, supplier_id)).fetchone()
            if dup is not None:
                raise WorkflowError(Reason.QUOTE_DUPLICATE,
                                    "supplier already quoted for this requisition")
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS s FROM quotes WHERE requisition_id=?",
                (requisition_id,)).fetchone()["s"]
            conn.execute(
                "INSERT INTO quotes (id, requisition_id, supplier_id, amount,"
                " valid_until, status, seq) VALUES (?,?,?,?,?,?,?)",
                (qid, requisition_id, supplier_id, amount, valid_until,
                 QuoteStatus.SUBMITTED.value, seq))
            self._store.emit(conn, "quote", qid, "submitted", payload)
            return {"id": qid, "status": QuoteStatus.SUBMITTED.value, "seq": seq}

        return _run_idempotent(self._store, key, payload, fn)

    def close(self, requisition_id: str, now: str, key: str | None = None) -> dict:
        """Adjudicate quotes. Deterministic: lowest valid amount, then arrival."""
        def fn(conn):
            req = self._requisitions._row(conn, requisition_id)
            status = RequisitionStatus(req["status"])
            assert_transition(REQUISITION_TRANSITIONS, status,
                              RequisitionStatus.QUOTE_CLOSED,
                              "requisition", requisition_id)
            rows = conn.execute(
                "SELECT * FROM quotes WHERE requisition_id=? ORDER BY seq",
                (requisition_id,)).fetchall()
            valid, expired = [], []
            for r in rows:
                (valid if r["valid_until"] >= now else expired).append(r)
            if not valid:
                raise WorkflowError(Reason.PRECONDITION_MISSING,
                                    "no valid quotes to adjudicate")
            winner = min(valid, key=lambda r: (r["amount"], r["seq"]))
            for r in valid:
                st = QuoteStatus.ACCEPTED if r["id"] == winner["id"] else QuoteStatus.REJECTED
                conn.execute("UPDATE quotes SET status=? WHERE id=?",
                             (st.value, r["id"]))
            for r in expired:
                conn.execute("UPDATE quotes SET status=? WHERE id=?",
                             (QuoteStatus.LATE.value, r["id"]))
            rationale = {
                "rule": self.ADJUDICATION_RULE,
                "now": now,
                "winner": {"quote_id": winner["id"], "supplier_id": winner["supplier_id"],
                           "amount": winner["amount"], "seq": winner["seq"]},
                "candidates": [
                    {"quote_id": r["id"], "supplier_id": r["supplier_id"],
                     "amount": r["amount"], "seq": r["seq"],
                     "valid": r["valid_until"] >= now}
                    for r in rows],
            }
            self._requisitions._force_transition(
                conn, requisition_id, status, RequisitionStatus.QUOTE_CLOSED,
                "quotes_adjudicated", rationale)
            return {"requisition_id": requisition_id,
                    "status": RequisitionStatus.QUOTE_CLOSED.value,
                    "decision": rationale}

        return _run_idempotent(self._store, key,
                               {"req": requisition_id, "now": now}, fn)

    def list_for(self, requisition_id: str) -> list[dict]:
        return [dict(r) for r in self._store.all(
            "SELECT * FROM quotes WHERE requisition_id=? ORDER BY seq",
            (requisition_id,))]


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ApprovalPolicy:
    """Amount-tiered approval chain. First tier whose ceiling covers the
    amount defines the ordered roles that must approve."""
    tiers: tuple[tuple[float, tuple[Role, ...]], ...] = (
        (10_000.0, (Role.MANAGER,)),
        (100_000.0, (Role.MANAGER, Role.DIRECTOR)),
        (float("inf"), (Role.MANAGER, Role.DIRECTOR)),
    )

    def chain_for(self, amount: float) -> tuple[Role, ...]:
        for ceiling, roles in self.tiers:
            if amount <= ceiling:
                return roles
        return self.tiers[-1][1]


class ApprovalService:
    def __init__(self, store: Store, requisitions: RequisitionService,
                 policy: ApprovalPolicy | None = None) -> None:
        self._store = store
        self._requisitions = requisitions
        self._policy = policy or ApprovalPolicy()

    def start(self, requisition_id: str, assignees: dict[str, str] | None = None,
              key: str | None = None) -> dict:
        """Build the approval chain from the accepted quote amount."""
        assignees = assignees or {}

        def fn(conn):
            req = self._requisitions._row(conn, requisition_id)
            status = RequisitionStatus(req["status"])
            assert_transition(REQUISITION_TRANSITIONS, status,
                              RequisitionStatus.APPROVING,
                              "requisition", requisition_id)
            quote = conn.execute(
                "SELECT * FROM quotes WHERE requisition_id=? AND status=?",
                (requisition_id, QuoteStatus.ACCEPTED.value)).fetchone()
            if quote is None:
                raise WorkflowError(Reason.PRECONDITION_MISSING,
                                    "no accepted quote to approve")
            chain = self._policy.chain_for(quote["amount"])
            steps = []
            for i, role in enumerate(chain, start=1):
                aid = _new_id()
                assignee = assignees.get(role.value, "")
                conn.execute(
                    "INSERT INTO approvals (id, requisition_id, step, role,"
                    " assignee, status) VALUES (?,?,?,?,?,?)",
                    (aid, requisition_id, i, role.value, assignee,
                     ApprovalStatus.PENDING.value))
                steps.append({"id": aid, "step": i, "role": role.value,
                              "assignee": assignee})
            self._requisitions._force_transition(
                conn, requisition_id, status, RequisitionStatus.APPROVING,
                "approval_chain_started",
                {"amount": quote["amount"], "steps": steps})
            return {"requisition_id": requisition_id, "steps": steps}

        return _run_idempotent(self._store, key,
                               {"req": requisition_id, "assignees": assignees}, fn)

    def _decide(self, approval_id: str, actor_id: str, actor_role: Role,
                approve: bool, key: str | None) -> dict:
        action = "approved" if approve else "rejected"

        def fn(conn):
            row = conn.execute("SELECT * FROM approvals WHERE id=?",
                               (approval_id,)).fetchone()
            if row is None:
                raise WorkflowError(Reason.NOT_FOUND,
                                    f"approval {approval_id} not found")
            if row["status"] != ApprovalStatus.PENDING.value:
                raise WorkflowError(Reason.APPROVAL_NOT_PENDING,
                                    f"approval is {row['status']}")
            if row["role"] != actor_role.value:
                raise WorkflowError(
                    Reason.APPROVAL_FORBIDDEN,
                    f"step requires {row['role']}, actor is {actor_role.value}")
            if row["assignee"] and row["assignee"] != actor_id:
                raise WorkflowError(
                    Reason.APPROVAL_FORBIDDEN,
                    f"step is assigned to {row['assignee']}, not {actor_id}")
            new_status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
            conn.execute("UPDATE approvals SET status=? WHERE id=?",
                         (new_status.value, approval_id))
            self._store.emit(conn, "approval", approval_id, action,
                             {"actor": actor_id, "role": actor_role.value})
            req_id = row["requisition_id"]
            req_status = RequisitionStatus(conn.execute(
                "SELECT status FROM requisitions WHERE id=?", (req_id,)).fetchone()["status"])
            if not approve:
                self._requisitions._force_transition(
                    conn, req_id, req_status, RequisitionStatus.REJECTED,
                    "approval_rejected", {"approval_id": approval_id})
            else:
                pending = conn.execute(
                    "SELECT COUNT(*) AS c FROM approvals WHERE requisition_id=?"
                    " AND status=?", (req_id, ApprovalStatus.PENDING.value)).fetchone()["c"]
                if pending == 0:
                    self._requisitions._force_transition(
                        conn, req_id, req_status, RequisitionStatus.APPROVED,
                        "approval_chain_completed", {"requisition_id": req_id})
            return {"id": approval_id, "status": new_status.value}

        return _run_idempotent(self._store, key,
                               {"approval": approval_id, "actor": actor_id,
                                "approve": approve}, fn)

    def approve(self, approval_id: str, actor_id: str, actor_role: Role,
                key: str | None = None) -> dict:
        return self._decide(approval_id, actor_id, actor_role, True, key)

    def reject(self, approval_id: str, actor_id: str, actor_role: Role,
               key: str | None = None) -> dict:
        return self._decide(approval_id, actor_id, actor_role, False, key)

    def transfer(self, approval_id: str, new_assignee: str,
                 key: str | None = None) -> dict:
        """Hand a pending approval to someone else (e.g. assignee left).

        The old row is kept as TRANSFERRED so the chain stays traceable and
        a fresh PENDING row carries the same step — nothing is lost.
        """
        def fn(conn):
            row = conn.execute("SELECT * FROM approvals WHERE id=?",
                               (approval_id,)).fetchone()
            if row is None:
                raise WorkflowError(Reason.NOT_FOUND,
                                    f"approval {approval_id} not found")
            if row["status"] != ApprovalStatus.PENDING.value:
                raise WorkflowError(Reason.APPROVAL_NOT_PENDING,
                                    f"cannot transfer a {row['status']} approval")
            conn.execute("UPDATE approvals SET status=? WHERE id=?",
                         (ApprovalStatus.TRANSFERRED.value, approval_id))
            new_id = _new_id()
            conn.execute(
                "INSERT INTO approvals (id, requisition_id, step, role, assignee,"
                " status) VALUES (?,?,?,?,?,?)",
                (new_id, row["requisition_id"], row["step"], row["role"],
                 new_assignee, ApprovalStatus.PENDING.value))
            detail = {"from_approval": approval_id, "to_approval": new_id,
                      "new_assignee": new_assignee}
            self._store.emit(conn, "approval", approval_id, "transferred", detail)
            self._store.emit(conn, "approval", new_id, "created_by_transfer", detail)
            return {"id": new_id, "step": row["step"], "role": row["role"],
                    "assignee": new_assignee, "status": ApprovalStatus.PENDING.value}

        return _run_idempotent(self._store, key,
                               {"approval": approval_id, "to": new_assignee}, fn)

    def list_for(self, requisition_id: str) -> list[dict]:
        return [dict(r) for r in self._store.all(
            "SELECT * FROM approvals WHERE requisition_id=? ORDER BY step, rowid",
            (requisition_id,))]


# --------------------------------------------------------------------------
# Purchase orders (versioned)
# --------------------------------------------------------------------------
class OrderService:
    def __init__(self, store: Store, suppliers: SupplierService,
                 requisitions: RequisitionService) -> None:
        self._store = store
        self._suppliers = suppliers
        self._requisitions = requisitions

    def create_from_requisition(self, requisition_id: str, po_id: str | None = None,
                                key: str | None = None) -> dict:
        pid = po_id or _new_id()

        def fn(conn):
            req = self._requisitions._row(conn, requisition_id)
            status = RequisitionStatus(req["status"])
            assert_transition(REQUISITION_TRANSITIONS, status,
                              RequisitionStatus.ORDERED,
                              "requisition", requisition_id)
            existing = conn.execute(
                "SELECT id FROM purchase_orders WHERE requisition_id=?",
                (requisition_id,)).fetchone()
            if existing is not None:
                raise WorkflowError(Reason.DUPLICATE_SUBMISSION,
                                    "a purchase order already exists for this requisition")
            quote = conn.execute(
                "SELECT * FROM quotes WHERE requisition_id=? AND status=?",
                (requisition_id, QuoteStatus.ACCEPTED.value)).fetchone()
            if quote is None:
                raise WorkflowError(Reason.PRECONDITION_MISSING,
                                    "no accepted quote to order from")
            # Supplier status is re-checked at order time: suspended suppliers
            # must not receive new orders even if their quote won earlier.
            self._suppliers.require_quotable(conn, quote["supplier_id"])
            conn.execute(
                "INSERT INTO purchase_orders (id, requisition_id, supplier_id,"
                " amount, status, version) VALUES (?,?,?,?,?,1)",
                (pid, requisition_id, quote["supplier_id"], quote["amount"],
                 POStatus.DRAFT.value))
            self._snapshot(conn, pid)
            self._requisitions._force_transition(
                conn, requisition_id, status, RequisitionStatus.ORDERED,
                "purchase_order_created", {"po_id": pid})
            self._store.emit(conn, "purchase_order", pid, "created",
                             {"requisition_id": requisition_id})
            return {"id": pid, "status": POStatus.DRAFT.value, "version": 1}

        return _run_idempotent(self._store, key,
                               {"req": requisition_id, "id": pid}, fn)

    def _row(self, conn, po_id: str):
        row = conn.execute("SELECT * FROM purchase_orders WHERE id=?",
                           (po_id,)).fetchone()
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND, f"purchase order {po_id} not found")
        return row

    def _snapshot(self, conn, po_id: str) -> None:
        row = self._row(conn, po_id)
        payload = {"supplier_id": row["supplier_id"], "amount": row["amount"],
                   "status": row["status"], "version": row["version"]}
        conn.execute(
            "INSERT INTO po_versions (po_id, version, payload) VALUES (?,?,?)",
            (po_id, row["version"], json.dumps(payload)))

    def get(self, po_id: str) -> dict:
        row = self._store.one("SELECT * FROM purchase_orders WHERE id=?", (po_id,))
        if row is None:
            raise WorkflowError(Reason.NOT_FOUND, f"purchase order {po_id} not found")
        return dict(row)

    def advance(self, po_id: str, target: POStatus, key: str | None = None) -> dict:
        def fn(conn):
            row = self._row(conn, po_id)
            current = POStatus(row["status"])
            assert_transition(PO_TRANSITIONS, current, target,
                              "purchase_order", po_id)
            conn.execute(
                "UPDATE purchase_orders SET status=?, version=version+1 WHERE id=?",
                (target.value, po_id))
            self._snapshot(conn, po_id)
            self._store.emit(conn, "purchase_order", po_id, "transition",
                             {"from": current.value, "to": target.value})
            return {"id": po_id, "status": target.value}

        return _run_idempotent(self._store, key,
                               {"po": po_id, "target": target.value}, fn)

    def amend(self, po_id: str, base_version: int, changes: dict,
              key: str | None = None) -> dict:
        """Whole-document optimistic concurrency: the first committer wins,
        the loser gets VERSION_CONFLICT — fields are never merged."""
        allowed_fields = {"amount", "supplier_id"}

        def fn(conn):
            row = self._row(conn, po_id)
            if POStatus(row["status"]) not in (POStatus.DRAFT, POStatus.SUBMITTED):
                raise WorkflowError(Reason.ILLEGAL_TRANSITION,
                                    f"cannot amend a {row['status']} purchase order")
            if row["version"] != base_version:
                raise WorkflowError(
                    Reason.VERSION_CONFLICT,
                    f"expected version {base_version}, current is {row['version']}")
            unknown = set(changes) - allowed_fields
            if unknown:
                raise WorkflowError(Reason.PRECONDITION_MISSING,
                                    f"fields not amendable: {sorted(unknown)}")
            if "supplier_id" in changes:
                self._suppliers.require_quotable(conn, changes["supplier_id"])
            sets = ", ".join(f"{f}=?" for f in changes)
            conn.execute(
                f"UPDATE purchase_orders SET {sets}, version=version+1 WHERE id=?",
                (*changes.values(), po_id))
            self._snapshot(conn, po_id)
            self._store.emit(conn, "purchase_order", po_id, "amended",
                             {"base_version": base_version, "changes": changes})
            return self.get(po_id)

        return _run_idempotent(self._store, key,
                               {"po": po_id, "base": base_version, "changes": changes}, fn)

    def rollback(self, po_id: str, target_version: int,
                 key: str | None = None) -> dict:
        """Restore a historical version as a NEW version, so behaviour after
        rollback matches the document exactly as it was then."""
        def fn(conn):
            row = self._row(conn, po_id)
            ver = conn.execute(
                "SELECT payload FROM po_versions WHERE po_id=? AND version=?",
                (po_id, target_version)).fetchone()
            if ver is None:
                raise WorkflowError(Reason.NOT_FOUND,
                                    f"version {target_version} not found")
            payload = json.loads(ver["payload"])
            if POStatus(row["status"]) not in (POStatus.DRAFT, POStatus.SUBMITTED):
                raise WorkflowError(Reason.ILLEGAL_TRANSITION,
                                    f"cannot roll back a {row['status']} purchase order")
            self._suppliers.require_quotable(conn, payload["supplier_id"])
            conn.execute(
                "UPDATE purchase_orders SET supplier_id=?, amount=?,"
                " version=version+1 WHERE id=?",
                (payload["supplier_id"], payload["amount"], po_id))
            self._snapshot(conn, po_id)
            self._store.emit(conn, "purchase_order", po_id, "rolled_back",
                             {"to_version": target_version})
            return self.get(po_id)

        return _run_idempotent(self._store, key,
                               {"po": po_id, "version": target_version}, fn)

    def versions(self, po_id: str) -> list[dict]:
        rows = self._store.all(
            "SELECT version, payload FROM po_versions WHERE po_id=? ORDER BY version",
            (po_id,))
        return [{"version": r["version"], "payload": json.loads(r["payload"])}
                for r in rows]
