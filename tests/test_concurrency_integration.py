"""跨阶段并发：报价与审批交错、重复下单幂等、并发下结果唯一确定。"""

from __future__ import annotations

import threading

from procurement.errors import (
    IllegalTransitionError,
    QuoteRejectedError,
    UnauthorizedApprovalError,
)

from .conftest import seed_ready_to_quote


def test_quotes_arriving_concurrently_with_close_are_deterministic(svc, clock):
    """多个报价并发提交、随后截止裁决：
    - 先于裁决提交的进入有效集参与裁决；
    - 裁决后到达的成为 LATE（不改变结果）；
    - 中标结果唯一确定，重复裁决被拒绝。"""

    seed_ready_to_quote(svc, suppliers=[f"s{i}" for i in range(1, 5)])
    outcomes: dict[str, str] = {}
    quote_barrier = threading.Barrier(3)

    def quote(qid: str, sid: str, amount: float) -> None:
        quote_barrier.wait()
        try:
            svc.submit_quote(qid, "r1", sid, amount, "2027-03-01T00:00:00+00:00",
                             accept_late=True)
            outcomes[qid] = "accepted"
        except QuoteRejectedError:
            outcomes[qid] = "rejected"

    ts = [
        threading.Thread(target=quote, args=("q1", "s1", 50000)),
        threading.Thread(target=quote, args=("q2", "s2", 30000)),
        threading.Thread(target=quote, args=("q3", "s3", 40000)),
    ]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    svc.close_quoting("r1", "buyer")
    outcomes["close"] = "awarded"
    # 裁决完成后，一个更低价报价才到达 -> LATE，不改变中标
    svc.submit_quote("q-late", "r1", "s4", 1, "2027-03-01T00:00:00+00:00",
                     accept_late=True)
    req = svc.get_requisition("r1")
    assert req["award"] is not None
    accepted = [q for q in req["quotes"] if q["status"] in ("WINNING", "LOSING")]
    late = [q for q in req["quotes"] if q["status"] == "LATE"]
    # 中标者一定是被接受报价中的最低价
    winner = min(accepted, key=lambda q: q["amount"])
    assert req["award"]["winning_quote_id"] == winner["id"]
    assert len(accepted) + len(late) == 4
    # 再裁决一次必须报错（状态已推进），结果不被二次计算
    try:
        svc.close_quoting("r1", "buyer")
        assert False, "重复裁决必须被拒绝"
    except IllegalTransitionError:
        pass


def test_concurrent_order_creation_idempotent(svc):
    """两个线程同时为同一需求点"下单"且携带相同幂等键：只产生一张采购单。"""

    seed_ready_to_quote(svc, suppliers=("s1",), amount=5000)
    svc.submit_quote("q1", "r1", "s1", 5000, "2027-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    svc.approve("r1", "alice")

    results: list[str] = []
    barrier = threading.Barrier(2)

    def create(tag: str) -> None:
        barrier.wait()
        try:
            out = svc.create_order(
                "po1", "r1", 5000, "item", 1, "2027-04-01T00:00:00+00:00",
                idem_key="create-po-r1")
            results.append(out["status"])
        except IllegalTransitionError as ex:
            results.append(ex.code)

    t1 = threading.Thread(target=create, args=("A",))
    t2 = threading.Thread(target=create, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()
    # 一个真正提交，一个幂等回放
    assert sorted(results) == ["idempotent_replay", "ok"]
    assert len(svc.list_orders()) == 1
    assert svc.get_requisition("r1")["purchase_order_id"] == "po1"


def test_approval_while_supplier_quote_inflight_does_not_corrupt(svc, clock):
    """报价与审批处于不同阶段：AWARDED 后到达的报价不能影响进行中的审批链。"""

    seed_ready_to_quote(svc, suppliers=("s1", "s2"), amount=150000)
    svc.submit_quote("q1", "r1", "s1", 150000, "2027-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    # 审批进行中，另一家以更低价报价 -> LATE，审批照常
    svc.submit_quote("q-late", "r1", "s2", 1000, "2027-03-01T00:00:00+00:00")
    svc.approve("r1", "alice")
    svc.approve("r1", "bob")
    req = svc.get_requisition("r1")
    assert req["award"]["winning_supplier_id"] == "s1"
    assert req["status"] == "APPROVING"  # 还剩 vp
    svc.approve("r1", "carol")
    assert svc.get_requisition("r1")["status"] == "APPROVED"
    # 流程已结束，再审批被拒绝
    try:
        svc.approve("r1", "alice")
        assert False
    except (UnauthorizedApprovalError, IllegalTransitionError):
        pass
