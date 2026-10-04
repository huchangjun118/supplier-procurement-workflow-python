"""需求 6：供应商准入/停用状态对新业务的门禁，历史单据不受影响且可查拒绝原因。"""

from __future__ import annotations

import pytest

from procurement.errors import (
    IllegalTransitionError,
    SupplierDisabledError,
    SupplierNotAdmittedError,
)

from .conftest import seed_ready_to_quote


def test_pending_supplier_cannot_quote_or_be_awarded(svc):
    svc.register_supplier("s1", "A")  # 注册后默认 PENDING
    svc.register_supplier("s2", "B"); svc.admit_supplier("s2")
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    with pytest.raises(SupplierNotAdmittedError):
        svc.submit_quote("q1", "r1", "s1", 1000, "2026-03-01T00:00:00+00:00")
    assert svc.get_supplier("s1")["can_participate"] is False


def test_rejected_supplier_reason_recorded(svc):
    svc.register_supplier("s1", "A")
    svc.reject_supplier("s1", "资质文件缺失")
    view = svc.get_supplier("s1")
    assert view["status"] == "REJECTED"
    assert view["reject_reason"] == "资质文件缺失"
    assert view["can_participate"] is False


def test_disabled_supplier_blocked_from_new_business(svc):
    seed_ready_to_quote(svc, suppliers=("s1", "s2"))
    svc.disable_supplier("s1", "抽检不合格")
    # 新报价被拒，原因可查
    with pytest.raises(SupplierDisabledError) as ei:
        svc.submit_quote("q1", "r1", "s1", 1000, "2026-03-01T00:00:00+00:00")
    assert "抽检不合格" in ei.value.message
    assert svc.get_supplier("s1")["reject_reason"] == "抽检不合格"


def test_disabling_after_award_blocks_new_order(svc):
    """已中标但下单前供应商被停用：新采购单必须被拦截（防止向停用供应商下单）。"""

    seed_ready_to_quote(svc, suppliers=("s1",))
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    svc.approve("r1", "alice")
    svc.disable_supplier("s1", "突发合规问题")
    with pytest.raises(SupplierDisabledError):
        svc.create_order("po1", "r1", 5000, "item", 1, "2026-04-01T00:00:00+00:00")
    assert svc.get_requisition("r1")["purchase_order_id"] is None  # 没有产生单据


def test_existing_order_unaffected_when_supplier_disabled(svc):
    """历史单据不受影响：停用前已生成的采购单仍可查询、变更、完结。"""

    seed_ready_to_quote(svc, suppliers=("s1",))
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "buyer")
    svc.start_approval("r1", "buyer")
    svc.approve("r1", "alice")
    svc.create_order("po1", "r1", 5000, "item", 1, "2026-04-01T00:00:00+00:00")
    svc.disable_supplier("s1", "事后停用")
    # 历史单据照常查询
    po = svc.get_order("po1")
    assert po["supplier_id"] == "s1" and po["version"] == 1
    # 历史单据的变更/完结不受供应商新状态影响
    svc.change_order("po1", {"note": "补充交付说明"}, "历史单据维护", "buyer",
                     expected_version=1)
    svc.complete_order("po1", "buyer")
    assert svc.get_requisition("r1")["status"] == "CLOSED"
    # 但新需求不能再让它报价
    svc.create_requisition("r2", "t2", "u", 100)
    svc.submit_requisition("r2", "u")
    svc.open_quoting("r2", "u", deadline="2026-02-10T00:00:00+00:00")
    with pytest.raises(SupplierDisabledError):
        svc.submit_quote("q9", "r2", "s1", 100, "2026-03-01T00:00:00+00:00")


def test_re_enable_supplier_restores_participation(svc):
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.disable_supplier("s1", "临时整改")
    with pytest.raises(IllegalTransitionError):
        svc.admit_supplier("s1")  # 停用状态不能走准入接口
    svc.enable_supplier("s1")
    assert svc.get_supplier("s1")["status"] == "ADMITTED"
    # 重新参与新业务
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")  # 不抛异常
    assert svc.get_requisition("r1")["quotes"][0]["status"] == "SUBMITTED"


def test_status_history_traceable(svc):
    svc.register_supplier("s1", "A")
    svc.admit_supplier("s1")
    svc.disable_supplier("s1", "整改")
    svc.enable_supplier("s1")
    statuses = [h["status"] for h in svc.get_supplier("s1")["status_history"]]
    assert statuses[0] == "PENDING"
    assert "ADMITTED" in statuses and "DISABLED" in statuses
    assert statuses[-1] == "ADMITTED"


def test_disable_requires_reason(svc):
    from procurement.errors import PreconditionFailedError
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    with pytest.raises(PreconditionFailedError):
        svc.disable_supplier("s1", "")
