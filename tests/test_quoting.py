"""需求 2：多供应商并发报价、确定性裁决、迟到/重复报价不偏移结果、裁决可解释。"""

from __future__ import annotations

import threading

import pytest

from procurement.errors import (
    DuplicateQuoteError,
    QuoteRejectedError,
    SupplierDisabledError,
    SupplierNotAdmittedError,
)

from .conftest import seed_ready_to_quote


def _award_to(svc, req_id="r1") -> tuple[str, float]:
    award = svc.get_requisition(req_id)["award"]
    return award["winning_supplier_id"], award["amount"]


def test_concurrent_quotes_all_accepted_and_award_deterministic(svc, clock):
    seed_ready_to_quote(svc, suppliers=[f"s{i}" for i in range(1, 6)])
    amounts = {"s1": 90000, "s2": 50000, "s3": 70000, "s4": 50000, "s5": 30000}
    barrier = threading.Barrier(5)
    errors: list[Exception] = []

    def worker(sid: str) -> None:
        barrier.wait()
        try:
            svc.submit_quote(f"q-{sid}", "r1", sid, amounts[sid],
                             "2026-03-01T00:00:00+00:00")
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(sid,))
               for sid in amounts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    svc.close_quoting("r1", "buyer")
    winner, amount = _award_to(svc)
    assert winner == "s5" and amount == 30000
    req = svc.get_requisition("r1")
    assert len(req["quotes"]) == 5
    assert {q["status"] for q in req["quotes"] if q["supplier_id"] != "s5"} == {"LOSING"}


def test_tie_break_by_submission_time_then_supplier_id(svc, clock):
    seed_ready_to_quote(svc, suppliers=("s1", "s2"))
    svc.submit_quote("q1", "r1", "s1", 50000, "2026-03-01T00:00:00+00:00")
    clock.set("2026-01-02T00:00:00+00:00")
    svc.submit_quote("q2", "r1", "s2", 50000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    winner, _ = _award_to(svc)
    assert winner == "s1"  # 同金额，先提交者胜


def test_duplicate_quote_from_same_supplier_rejected(svc):
    seed_ready_to_quote(svc, suppliers=("s1",))
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    with pytest.raises(DuplicateQuoteError) as ei:
        svc.submit_quote("q2", "r1", "s1", 4000, "2026-03-01T00:00:00+00:00")
    assert ei.value.code == "DUPLICATE_QUOTE"
    # 原报价不变
    req = svc.get_requisition("r1")
    assert len(req["quotes"]) == 1
    assert req["quotes"][0]["amount"] == 5000


def test_late_quote_recorded_but_never_shifts_award(svc, clock):
    seed_ready_to_quote(svc, suppliers=("s1", "s2"))
    svc.submit_quote("q1", "r1", "s1", 50000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    assert _award_to(svc) == ("s1", 50000)
    # 裁决后更低报价迟到
    clock.set("2026-01-09T00:00:00+00:00")
    svc.submit_quote("q-late", "r1", "s2", 1000, "2026-03-01T00:00:00+00:00")
    assert _award_to(svc) == ("s1", 50000)
    req = svc.get_requisition("r1")
    late = [q for q in req["quotes"] if q["id"] == "q-late"][0]
    assert late["status"] == "LATE"
    assert "q-late" in req["award"]["rejected_late_quote_ids"]
    # 后续审批/下单仍指向原中标方
    svc.start_approval("r1", "buyer")
    svc.approve("r1", "alice")
    svc.approve("r1", "bob")
    svc.create_order("po1", "r1", 50000, "item", 1, "2026-04-01T00:00:00+00:00")
    assert svc.get_order("po1")["supplier_id"] == "s1"


def test_expired_quote_excluded_from_award(svc, clock):
    seed_ready_to_quote(svc, suppliers=("s1", "s2"))
    svc.submit_quote("q1", "r1", "s1", 50000, "2026-01-20T00:00:00+00:00")
    clock.set("2026-01-05T00:00:00+00:00")
    svc.submit_quote("q2", "r1", "s2", 10000, "2026-03-01T00:00:00+00:00")
    clock.set("2026-02-01T00:00:00+00:00")  # s1 报价已过期
    svc.close_quoting("r1", "buyer")
    winner, amount = _award_to(svc)
    assert winner == "s2"  # 更便宜但过期的 s1 被排除


def test_quote_validity_must_be_in_future_on_submission(svc):
    seed_ready_to_quote(svc, suppliers=("s1",))
    with pytest.raises(QuoteRejectedError) as ei:
        svc.submit_quote("q1", "r1", "s1", 5000, "2025-12-31T00:00:00+00:00")
    assert ei.value.code == "QUOTE_VALIDITY_EXPIRED"


def test_quote_after_deadline_rejected_before_award(svc, clock):
    seed_ready_to_quote(svc, suppliers=("s1",))
    clock.set("2026-02-11T00:00:00+00:00")  # 晚于报价截止 2026-02-10
    with pytest.raises(QuoteRejectedError) as ei:
        svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    assert ei.value.code == "QUOTE_DEADLINE_PASSED"


def test_pending_and_disabled_supplier_cannot_quote(svc):
    svc.register_supplier("s1", "A")
    svc.register_supplier("s2", "B")
    svc.admit_supplier("s2")
    svc.create_requisition("r1", "t", "u", 1000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    # 未准入
    with pytest.raises(SupplierNotAdmittedError):
        svc.submit_quote("q1", "r1", "s1", 500, "2026-03-01T00:00:00+00:00")
    # 停用
    svc.disable_supplier("s2", "考核不达标")
    with pytest.raises(SupplierDisabledError) as ei:
        svc.submit_quote("q2", "r1", "s2", 500, "2026-03-01T00:00:00+00:00")
    assert "考核不达标" in ei.value.message


def test_award_reason_is_explainable(svc):
    seed_ready_to_quote(svc, suppliers=("s1", "s2", "s3"))
    svc.submit_quote("q1", "r1", "s1", 50000, "2026-03-01T00:00:00+00:00")
    svc.submit_quote("q2", "r1", "s2", 30000, "2026-03-01T00:00:00+00:00")
    svc.submit_quote("q3", "r1", "s3", 40000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    reason = svc.get_requisition("r1")["award"]["reason"]
    assert "q2" in reason and "最低金额" in reason
    assert "q1" in reason and "q3" in reason
