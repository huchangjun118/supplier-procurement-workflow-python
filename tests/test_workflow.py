"""Scenario tests: concurrency, idempotency, authorization, recovery,
supplier lifecycle, and versioned purchase orders."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.errors import Reason, WorkflowError
from app.models import POStatus, RequisitionStatus, Role
from app.services import (
    ApprovalService,
    OrderService,
    QuoteService,
    RequisitionService,
    SupplierService,
)
from app.store import Store


@pytest.fixture()
def svc():
    store = Store(":memory:")
    suppliers = SupplierService(store)
    requisitions = RequisitionService(store)
    quotes = QuoteService(store, suppliers, requisitions)
    approvals = ApprovalService(store, requisitions)
    orders = OrderService(store, suppliers, requisitions)
    yield store, suppliers, requisitions, quotes, approvals, orders
    store.close()


def _active_supplier(suppliers, sid):
    suppliers.register(name=sid, supplier_id=sid)
    suppliers.admit(sid)


def _quoting_req(requisitions, rid="r1", amount=5000.0):
    requisitions.create("req", amount, requisition_id=rid)
    requisitions.advance(rid, RequisitionStatus.SUBMITTED)
    requisitions.advance(rid, RequisitionStatus.QUOTING)
    return rid


def _approved_req(svc, rid="r1", amount=5000.0, quote_amount=4800.0):
    store, suppliers, requisitions, quotes, approvals, orders = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions, rid, amount)
    quotes.submit(rid, "s1", quote_amount, "2026-12-31")
    quotes.close(rid, "2026-10-01")
    chain = approvals.start(rid, {"MANAGER": "mgr", "DIRECTOR": "dir"})
    for step in chain["steps"]:
        approvals.approve(step["id"], step["assignee"] or "actor",
                          Role(step["role"]))
    return rid


# ---------------------------------------------------------------------------
# 1. State machine
# ---------------------------------------------------------------------------
def test_illegal_transition_rejected(svc):
    _, _, requisitions, *_ = svc
    requisitions.create("t", 100, requisition_id="r1")
    with pytest.raises(WorkflowError) as e:
        requisitions.advance("r1", RequisitionStatus.QUOTING)  # skips SUBMITTED
    assert e.value.reason is Reason.ILLEGAL_TRANSITION


def test_same_transition_twice_is_distinguishable_duplicate(svc):
    _, _, requisitions, *_ = svc
    requisitions.create("t", 100, requisition_id="r1")
    requisitions.advance("r1", RequisitionStatus.SUBMITTED)
    with pytest.raises(WorkflowError) as e:
        requisitions.advance("r1", RequisitionStatus.SUBMITTED)
    assert e.value.reason is Reason.DUPLICATE_SUBMISSION


def test_quote_before_window_open_is_precondition_missing(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    _active_supplier(suppliers, "s1")
    requisitions.create("t", 100, requisition_id="r1")
    with pytest.raises(WorkflowError) as e:
        quotes.submit("r1", "s1", 90, "2026-12-31")
    assert e.value.reason is Reason.PRECONDITION_MISSING


def test_idempotent_replay_returns_same_result(svc):
    _, _, requisitions, *_ = svc
    a = requisitions.create("t", 100, requisition_id="r1", key="k1")
    b = requisitions.create("t", 100, requisition_id="r1", key="k1")
    assert a == b
    assert requisitions.get("r1")["status"] == "DRAFT"


def test_idempotency_key_reuse_with_different_payload_conflicts(svc):
    _, _, requisitions, *_ = svc
    requisitions.create("t", 100, requisition_id="r1", key="k1")
    with pytest.raises(WorkflowError) as e:
        requisitions.create("other", 200, requisition_id="r2", key="k1")
    assert e.value.reason is Reason.IDEMPOTENCY_CONFLICT


# ---------------------------------------------------------------------------
# 2. Concurrent quotes and adjudication
# ---------------------------------------------------------------------------
def test_concurrent_quotes_adjudicated_by_amount_then_arrival(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    for s in ("s1", "s2", "s3"):
        _active_supplier(suppliers, s)
    _quoting_req(requisitions)

    with ThreadPoolExecutor(3) as pool:
        list(pool.map(
            lambda a: quotes.submit("r1", a[0], a[1], "2026-12-31"),
            [("s1", 500.0), ("s2", 450.0), ("s3", 450.0)]))

    decision = quotes.close("r1", "2026-10-01")["decision"]
    # s2 and s3 tie on amount; earliest arrival (lower seq) wins.
    assert decision["winner"]["amount"] == 450.0
    assert decision["rule"]
    statuses = {q["supplier_id"]: q["status"] for q in quotes.list_for("r1")}
    assert statuses[decision["winner"]["supplier_id"]] == "ACCEPTED"
    assert sorted(statuses.values()).count("REJECTED") == 2


def test_expired_quote_never_wins(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    _active_supplier(suppliers, "s1")
    _active_supplier(suppliers, "s2")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100.0, "2026-09-01")  # cheaper but expired
    quotes.submit("r1", "s2", 900.0, "2026-12-31")
    decision = quotes.close("r1", "2026-10-01")["decision"]
    assert decision["winner"]["supplier_id"] == "s2"
    statuses = {q["supplier_id"]: q["status"] for q in quotes.list_for("r1")}
    assert statuses["s1"] == "LATE"


def test_late_or_duplicate_quotes_cannot_shift_decided_result(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    _active_supplier(suppliers, "s1")
    _active_supplier(suppliers, "s2")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 500.0, "2026-12-31")
    quotes.submit("r1", "s2", 600.0, "2026-12-31")
    before = quotes.close("r1", "2026-10-01")["decision"]["winner"]

    with pytest.raises(WorkflowError) as e1:
        quotes.submit("r1", "s2", 1.0, "2027-01-01")  # duplicate supplier
    assert e1.value.reason is Reason.QUOTE_WINDOW_CLOSED  # window closed first
    _active_supplier(suppliers, "s3")
    with pytest.raises(WorkflowError) as e2:
        quotes.submit("r1", "s3", 1.0, "2027-01-01")  # late arrival
    assert e2.value.reason is Reason.QUOTE_WINDOW_CLOSED

    after = [q for q in quotes.list_for("r1") if q["status"] == "ACCEPTED"]
    assert len(after) == 1 and after[0]["id"] == before["quote_id"]


def test_duplicate_quote_same_window_rejected(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 500.0, "2026-12-31")
    with pytest.raises(WorkflowError) as e:
        quotes.submit("r1", "s1", 400.0, "2026-12-31")
    assert e.value.reason is Reason.QUOTE_DUPLICATE


# ---------------------------------------------------------------------------
# 3. Tiered approvals
# ---------------------------------------------------------------------------
def test_amount_tiers_define_chain(svc):
    _, suppliers, requisitions, quotes, approvals, _ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions, "big", amount=200_000)
    quotes.submit("big", "s1", 150_000, "2026-12-31")
    quotes.close("big", "2026-10-01")
    chain = approvals.start("big")
    assert [s["role"] for s in chain["steps"]] == ["MANAGER", "DIRECTOR"]


def test_overstep_approval_forbidden(svc):
    _, suppliers, requisitions, quotes, approvals, _ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    step = approvals.start("r1", {"MANAGER": "mgr"})["steps"][0]
    with pytest.raises(WorkflowError) as e1:
        approvals.approve(step["id"], "dir", Role.DIRECTOR)  # wrong role
    assert e1.value.reason is Reason.APPROVAL_FORBIDDEN
    with pytest.raises(WorkflowError) as e2:
        approvals.approve(step["id"], "intruder", Role.MANAGER)  # wrong assignee
    assert e2.value.reason is Reason.APPROVAL_FORBIDDEN


def test_concurrent_approval_of_same_step_resolves_once(svc):
    _, suppliers, requisitions, quotes, approvals, _ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    step = approvals.start("r1")["steps"][0]

    def try_approve(_):
        try:
            approvals.approve(step["id"], "mgr", Role.MANAGER)
            return "ok"
        except WorkflowError as e:
            return e.reason

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(try_approve, range(8)))
    assert results.count("ok") == 1
    assert results.count(Reason.APPROVAL_NOT_PENDING) == 7
    assert requisitions.get("r1")["status"] == "APPROVED"


def test_concurrent_full_chain_completes_exactly_once(svc):
    _, suppliers, requisitions, quotes, approvals, _ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions, amount=50_000)
    quotes.submit("r1", "s1", 40_000, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    steps = approvals.start("r1")["steps"]

    def try_approve(step):
        try:
            approvals.approve(step["id"], "a", Role(step["role"]))
            return "ok"
        except WorkflowError as e:
            return e.reason

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(try_approve, steps * 2))  # duplicates race too
    assert requisitions.get("r1")["status"] == "APPROVED"
    final = approvals.list_for("r1")
    assert all(a["status"] == "APPROVED" for a in final)


def test_transfer_preserves_chain_and_trace(svc):
    store, suppliers, requisitions, quotes, approvals, _ = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    step = approvals.start("r1", {"MANAGER": "leaver"})["steps"][0]

    moved = approvals.transfer(step["id"], "replacement")
    assert moved["assignee"] == "replacement"
    with pytest.raises(WorkflowError):  # leaver's record is closed
        approvals.approve(step["id"], "leaver", Role.MANAGER)
    approvals.approve(moved["id"], "replacement", Role.MANAGER)
    assert requisitions.get("r1")["status"] == "APPROVED"

    rows = approvals.list_for("r1")
    assert [r["status"] for r in rows].count("TRANSFERRED") == 1
    trail = store.events_for("approval", step["id"])
    assert any(e["action"] == "transferred" for e in trail)


# ---------------------------------------------------------------------------
# 4. Versioned purchase orders
# ---------------------------------------------------------------------------
def _po(svc, rid="r1"):
    *_, orders = svc
    _approved_req(svc, rid)
    return orders.create_from_requisition(rid)["id"]


def test_concurrent_amends_are_deterministic_no_field_merge(svc):
    pid = _po(svc)
    *_, orders = svc

    def try_amend(amount):
        try:
            orders.amend(pid, base_version=1, changes={"amount": amount})
            return "ok"
        except WorkflowError as e:
            return e.reason

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(try_amend, [111.0, 222.0]))
    assert results.count("ok") == 1
    assert results.count(Reason.VERSION_CONFLICT) == 1
    po = orders.get(pid)
    assert po["amount"] in (111.0, 222.0)  # whole document, never mixed


def test_versions_kept_and_rollback_restores_behaviour(svc):
    pid = _po(svc)
    *_, orders = svc
    original = orders.get(pid)["amount"]
    orders.amend(pid, 1, {"amount": 999.0})
    orders.amend(pid, 2, {"amount": 888.0})
    versions = orders.versions(pid)
    assert [v["version"] for v in versions] == [1, 2, 3]

    rolled = orders.rollback(pid, target_version=1)
    assert rolled["amount"] == original
    assert rolled["version"] == 4  # rollback is a new version, history kept
    assert len(orders.versions(pid)) == 4
    # behaviour after rollback matches v1: amending from v4 payload works
    again = orders.amend(pid, 4, {"amount": 777.0})
    assert again["amount"] == 777.0


def test_cannot_amend_confirmed_order(svc):
    pid = _po(svc)
    *_, orders = svc
    orders.advance(pid, POStatus.SUBMITTED)
    orders.advance(pid, POStatus.CONFIRMED)
    with pytest.raises(WorkflowError) as e:
        orders.amend(pid, orders.get(pid)["version"], {"amount": 1.0})
    assert e.value.reason is Reason.ILLEGAL_TRANSITION


# ---------------------------------------------------------------------------
# 5. Failure atomicity and restart recovery
# ---------------------------------------------------------------------------
def test_failed_step_leaves_no_partial_writes(svc):
    store, suppliers, requisitions, quotes, approvals, orders = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    approvals.start("r1")
    # close-quoting again must fail AND not corrupt state
    with pytest.raises(WorkflowError):
        quotes.close("r1", "2026-10-02")
    assert requisitions.get("r1")["status"] == "APPROVING"
    accepted = [q for q in quotes.list_for("r1") if q["status"] == "ACCEPTED"]
    assert len(accepted) == 1


def test_restart_recovers_committed_position(tmp_path):
    db = str(tmp_path / "wf.db")

    def build():
        store = Store(db)
        suppliers = SupplierService(store)
        requisitions = RequisitionService(store)
        quotes = QuoteService(store, suppliers, requisitions)
        approvals = ApprovalService(store, requisitions)
        orders = OrderService(store, suppliers, requisitions)
        return store, suppliers, requisitions, quotes, approvals, orders

    store, suppliers, requisitions, quotes, approvals, orders = build()
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    quotes.close("r1", "2026-10-01")
    store.close()  # "crash" after committed close, before approvals

    # after restart, committed state is visible and flow resumes from it
    store2, suppliers2, requisitions2, quotes2, approvals2, orders2 = build()
    assert requisitions2.get("r1")["status"] == "QUOTE_CLOSED"
    chain = approvals2.start("r1")["steps"]
    approvals2.approve(chain[0]["id"], "mgr", Role.MANAGER)
    po = orders2.create_from_requisition("r1")
    assert requisitions2.get("r1")["status"] == "ORDERED"
    assert orders2.get(po["id"])["status"] == "DRAFT"
    store2.close()


# ---------------------------------------------------------------------------
# 6. Supplier admission / suspension
# ---------------------------------------------------------------------------
def test_pending_supplier_cannot_quote(svc):
    _, suppliers, requisitions, quotes, *_ = svc
    suppliers.register("newbie", supplier_id="s9")
    _quoting_req(requisitions)
    with pytest.raises(WorkflowError) as e:
        quotes.submit("r1", "s9", 100, "2026-12-31")
    assert e.value.reason is Reason.SUPPLIER_NOT_ADMITTED


def test_suspended_supplier_cannot_quote_or_receive_new_orders(svc):
    _, suppliers, requisitions, quotes, approvals, orders = svc
    _active_supplier(suppliers, "s1")
    _quoting_req(requisitions)
    quotes.submit("r1", "s1", 100, "2026-12-31")
    suppliers.suspend("s1")
    with pytest.raises(WorkflowError) as e:
        quotes.submit("r1", "s1", 90, "2026-12-31")
    assert e.value.reason is Reason.SUPPLIER_SUSPENDED

    quotes.close("r1", "2026-10-01")  # existing quote still adjudicated
    chain = approvals.start("r1")["steps"]
    approvals.approve(chain[0]["id"], "m", Role.MANAGER)
    with pytest.raises(WorkflowError) as e2:
        orders.create_from_requisition("r1")  # suspended at order time
    assert e2.value.reason is Reason.SUPPLIER_SUSPENDED
    # failed order attempt left the requisition untouched
    assert requisitions.get("r1")["status"] == "APPROVED"


def test_suspension_does_not_affect_historical_orders(svc):
    _, suppliers, requisitions, quotes, approvals, orders = svc
    pid = _po(svc)
    orders.advance(pid, POStatus.SUBMITTED)
    suppliers.suspend("s1")
    po = orders.get(pid)  # historical document still readable
    assert po["status"] == "SUBMITTED" and po["supplier_id"] == "s1"
    events = svc[0].events_for("supplier", "s1")
    assert any(e["action"] == "status_changed" and e["detail"]["to"] == "SUSPENDED"
               for e in events)
