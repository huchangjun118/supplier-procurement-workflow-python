"""需求 3：分级审批、越权拒绝、审批转交不丢失、审批链可追溯，以及并发安全。"""

from __future__ import annotations

import threading

import pytest

from procurement.config import ApprovalTier, WorkflowConfig
from procurement.errors import (
    IllegalTransitionError,
    PreconditionFailedError,
    TransferError,
    UnauthorizedApprovalError,
)
from procurement.journal import Journal
from procurement.service import ProcurementService

from .conftest import FakeClock, seed_ready_to_quote


def reach_approval(svc, amount=150000):
    seed_ready_to_quote(svc, suppliers=("s1",), amount=amount)
    svc.submit_quote("q1", "r1", "s1", amount, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")


def test_tiers_generated_by_amount(svc):
    reach_approval(svc, amount=150000)
    nodes = svc.get_requisition("r1")["approvals"]
    assert [n["role"] for n in nodes] == ["manager", "director", "vp"]
    assert [n["approver"] for n in nodes] == ["alice", "bob", "carol"]


def test_low_amount_single_tier(svc):
    reach_approval(svc, amount=5000)
    nodes = svc.get_requisition("r1")["approvals"]
    assert [n["role"] for n in nodes] == ["manager"]


def test_unauthorized_approver_rejected(svc):
    reach_approval(svc)
    # bob 试图越过 manager 先批
    with pytest.raises(UnauthorizedApprovalError) as ei:
        svc.approve("r1", "bob")
    assert ei.value.code == "UNAUTHORIZED_APPROVAL"
    # 完全无关的人
    with pytest.raises(UnauthorizedApprovalError):
        svc.approve("r1", "eve")
    # 被拒绝后流程状态不变，当前节点仍等 alice
    nodes = svc.get_requisition("r1")["approvals"]
    assert nodes[0]["status"] == "PENDING"


def test_out_of_order_approval_rejected(svc):
    reach_approval(svc)
    svc.approve("r1", "alice")
    # 跳过 director 直接 vp
    with pytest.raises(UnauthorizedApprovalError):
        svc.approve("r1", "carol")


def test_duplicate_approval_is_idempotent(svc):
    reach_approval(svc, amount=5000)
    svc.approve("r1", "alice", comment="ok", idem_key="appr-1")
    before = len(svc.journal.all_events())
    again = svc.approve("r1", "alice", comment="ok", idem_key="appr-1")
    after = len(svc.journal.all_events())
    assert again["status"] == "idempotent_replay"
    assert before == after


def test_concurrent_approval_only_one_wins_per_node(svc):
    """同一节点两人同时点"通过"：真正的审批人成功一次，另一个被拒，节点不重复推进。"""

    reach_approval(svc, amount=5000)
    results: list[str] = []
    barrier = threading.Barrier(2)

    def approve(user: str) -> None:
        barrier.wait()
        try:
            svc.approve("r1", user)
            results.append(f"{user}:ok")
        except UnauthorizedApprovalError:
            results.append(f"{user}:denied")
        except Exception as ex:  # noqa: BLE001
            results.append(f"{user}:{type(ex).__name__}")

    t1 = threading.Thread(target=approve, args=("alice",))
    t2 = threading.Thread(target=approve, args=("eve",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert set(results) == {"alice:ok", "eve:denied"}
    nodes = svc.get_requisition("r1")["approvals"]
    assert [n["status"] for n in nodes] == ["APPROVED"]
    # 只有一个 NODE_APPROVED 事件
    kinds = [e.kind for e in svc.journal.events("requisition", "r1")]
    assert kinds.count("NODE_APPROVED") == 1


def test_concurrent_double_click_by_same_approver(svc):
    """审批人重复双击：串行化后只生效一次（第二次状态已推进，被拒绝/无待办）。"""

    reach_approval(svc, amount=5000)
    out: list[str] = []
    barrier = threading.Barrier(2)

    def click() -> None:
        barrier.wait()
        try:
            svc.approve("r1", "alice")
            out.append("ok")
        except PreconditionFailedError as ex:
            out.append(ex.code)
        except IllegalTransitionError as ex:
            out.append(ex.code)

    t1 = threading.Thread(target=click)
    t2 = threading.Thread(target=click)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert out.count("ok") == 1
    assert len(out) == 2 and out[1] in {"NO_PENDING_NODE", "ILLEGAL_TRANSITION"}
    kinds = [e.kind for e in svc.journal.events("requisition", "r1")]
    assert kinds.count("NODE_APPROVED") == 1


def test_transfer_then_new_approver_decides(svc):
    reach_approval(svc, amount=5000)
    # alice 离职，转交给同级别的 dave(manager)
    svc.transfer_approval("r1", "alice", "dave", "manager", "alice 离职交接")
    req = svc.get_requisition("r1")
    node = req["approvals"][0]
    assert node["approver"] == "dave"
    assert node["status"] == "PENDING"
    assert node["transfers"][0]["from_user"] == "alice"
    assert node["transfers"][0]["to_user"] == "dave"
    # 旧审批人不能再批
    with pytest.raises(UnauthorizedApprovalError):
        svc.approve("r1", "alice")
    # 新审批人通过
    svc.approve("r1", "dave")
    assert svc.get_requisition("r1")["status"] == "APPROVED"


def test_transfer_to_junior_role_rejected(svc):
    reach_approval(svc, amount=150000)
    # 第一节点 manager 不能转交给一个无审批权的角色
    with pytest.raises(TransferError):
        svc.transfer_approval("r1", "alice", "intern", "intern", "忙不过来")
    # vp 节点不能转交给 manager（越权降级）
    svc.approve("r1", "alice")
    svc.approve("r1", "bob")
    with pytest.raises(TransferError):
        svc.transfer_approval("r1", "carol", "alice", "manager", "降级转交")


def test_transfer_requires_current_approver(svc):
    reach_approval(svc, amount=5000)
    with pytest.raises(UnauthorizedApprovalError):
        svc.transfer_approval("r1", "eve", "dave", "manager", "代转交")


def test_rejection_marks_requisition_rejected(svc):
    reach_approval(svc)
    svc.approve("r1", "alice")
    with pytest.raises(PreconditionFailedError):
        svc.reject_approval("r1", "bob", comment="")  # 驳回必须有意见
    svc.reject_approval("r1", "bob", comment="价格不合理")
    req = svc.get_requisition("r1")
    assert req["status"] == "REJECTED"
    assert req["approvals"][1]["comment"] == "价格不合理"


def test_approval_chain_fully_traceable(svc):
    reach_approval(svc, amount=5000)
    svc.transfer_approval("r1", "alice", "dave", "manager", "离职")
    svc.approve("r1", "dave", comment="同意")
    trail = svc.audit_trail("requisition", "r1")
    kinds = [e["kind"] for e in trail]
    assert "APPROVAL_STARTED" in kinds
    assert "NODE_TRANSFERRED" in kinds
    assert "NODE_APPROVED" in kinds
    # 事件严格按 seq 递增
    seqs = [e["seq"] for e in trail]
    assert seqs == sorted(seqs)


def test_custom_tier_configuration():
    cfg = WorkflowConfig().with_tiers((
        ApprovalTier(1, "staff", 0.0, 1000.0, "u1"),
        ApprovalTier(2, "cfo", 1000.0, None, "u2"),
    ))
    svc = ProcurementService(Journal(), config=cfg, clock=FakeClock())
    reach_approval(svc, amount=5000)
    nodes = svc.get_requisition("r1")["approvals"]
    assert [n["role"] for n in nodes] == ["staff", "cfo"]
