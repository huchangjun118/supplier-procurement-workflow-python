"""需求 1：状态流转顺序、非法跳转可区分拒绝、动作幂等。"""

from __future__ import annotations

import pytest

from procurement.errors import (
    DuplicateSubmitError,
    IllegalTransitionError,
    PreconditionFailedError,
)
from procurement.models import RequisitionStatus

from .conftest import seed_ready_to_quote


def test_full_happy_path_transitions(svc, clock):
    seed_ready_to_quote(svc)
    svc.submit_quote("q1", "r1", "s1", 120000, "2026-03-01T00:00:00+00:00")
    clock.set("2026-01-05T00:00:00+00:00")
    svc.submit_quote("q2", "r1", "s2", 100000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    svc.approve("r1", "alice")
    svc.approve("r1", "bob")
    svc.approve("r1", "carol")
    svc.create_order("po1", "r1", 100000, "laptop", 10, "2026-04-01T00:00:00+00:00")
    svc.complete_order("po1", "buyer")
    assert svc.get_requisition("r1")["status"] == RequisitionStatus.CLOSED.value


def test_illegal_jump_draft_to_quoting_rejected(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    with pytest.raises(IllegalTransitionError) as ei:
        svc.open_quoting("r1", "u")
    assert ei.value.code == "ILLEGAL_TRANSITION"
    assert "DRAFT" in ei.value.message


def test_cannot_award_without_quotes(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    with pytest.raises(PreconditionFailedError) as ei:
        svc.close_quoting("r1", "u")
    assert ei.value.code == "NO_ELIGIBLE_QUOTE"


def test_illegal_jump_codes_are_distinguishable(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    # DRAFT 直接启动审批
    with pytest.raises(IllegalTransitionError):
        svc.start_approval("r1", "u")
    # 不存在的实体 -> ENTITY_NOT_FOUND
    from procurement.errors import EntityNotFoundError
    with pytest.raises(EntityNotFoundError) as ei:
        svc.get_requisition("nope")
    assert ei.value.code == "REQUISITION_NOT_FOUND"


def test_repeated_submit_is_idempotent(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    r1 = svc.submit_requisition("r1", "u", idem_key="k-submit")
    assert r1["status"] == "ok"
    # 相同幂等键重复提交 -> 回放，不产生新事件
    before = svc.journal.all_events()
    r2 = svc.submit_requisition("r1", "u", idem_key="k-submit")
    after = svc.journal.all_events()
    assert r2["status"] == "idempotent_replay"
    assert len(before) == len(after)


def test_repeated_submit_without_idem_key_is_still_noop(svc):
    """语义幂等：状态已在 SUBMITTED 时再次提交，返回空事件而非报错。"""

    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    svc.submit_requisition("r1", "u")
    result = svc.submit_requisition("r1", "u")
    assert result["events"] == 0


def test_duplicate_requisition_creation_rejected(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 1000)
    with pytest.raises(DuplicateSubmitError) as ei:
        svc.create_requisition("r1", "t2", "u2", 2000)
    assert ei.value.code == "DUPLICATE_SUBMIT"


def test_terminal_status_rejects_new_actions(svc, clock):
    seed_ready_to_quote(svc, suppliers=("s1",))
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    svc.reject_approval("r1", "alice", comment="预算不符")
    assert svc.get_requisition("r1")["status"] == RequisitionStatus.REJECTED.value
    # 终态再审批
    with pytest.raises(IllegalTransitionError):
        svc.approve("r1", "alice")
    # 终态取消也被拒绝
    with pytest.raises(IllegalTransitionError):
        svc.cancel_requisition("r1", "buyer")
