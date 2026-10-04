"""需求 4：采购单版本化、历史保留、并发冲突不发生字段混合、回滚行为与当时一致。"""

from __future__ import annotations

import threading

import pytest

from procurement.errors import (
    ConflictError,
    IllegalTransitionError,
    PreconditionFailedError,
    VersionNotFoundError,
)

from .conftest import seed_ready_to_quote


def place_order(svc, amount=5000, req_id="r1", order_id="po1"):
    seed_ready_to_quote(svc, suppliers=("s1",), amount=amount, req_id=req_id)
    svc.submit_quote("q1", req_id, "s1", amount, "2026-03-01T00:00:00+00:00")
    svc.close_quoting(req_id, "buyer")
    svc.start_approval(req_id, "buyer")
    svc.approve(req_id, "alice")
    svc.create_order(order_id, req_id, amount, "item", 10,
                     "2026-04-01T00:00:00+00:00", note="v1 note", actor="buyer")


def test_duplicate_order_creation_rejected(svc):
    place_order(svc)
    with pytest.raises(IllegalTransitionError) as ei:
        svc.create_order("po2", "r1", 5000, "x", 1, "2026-04-01T00:00:00+00:00")
    assert ei.value.code == "ALREADY_ORDERED"
    assert svc.get_requisition("r1")["purchase_order_id"] == "po1"


def test_change_creates_new_version_keeping_history(svc):
    place_order(svc)
    svc.change_order("po1", {"amount": 4500, "note": "谈下折扣"}, "供应商降价", "buyer",
                     expected_version=1)
    po = svc.get_order("po1")
    assert po["version"] == 2 and po["amount"] == 4500
    assert [v["version"] for v in po["versions"]] == [1, 2]
    assert po["versions"][0]["amount"] == 5000  # 历史版本原样保留
    assert po["versions"][1]["change_reason"] == "供应商降价"


def test_concurrent_changes_no_field_mixing(svc):
    """两个变更基于同一旧版本提交：一个成功，另一个必须收到冲突错误，
    不能出现"金额取 A、数量取 B"的字段级混合。"""

    place_order(svc)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def change(changes: dict, tag: str) -> None:
        barrier.wait()
        try:
            svc.change_order("po1", changes, tag, "buyer", expected_version=1)
            outcomes.append(f"{tag}:ok")
        except ConflictError:
            outcomes.append(f"{tag}:conflict")

    t1 = threading.Thread(target=change, args=({"amount": 1}, "A"))
    t2 = threading.Thread(target=change, args=({"quantity": 99}, "B"))
    t1.start(); t2.start(); t1.join(); t2.join()
    oks = [o for o in outcomes if o.endswith(":ok")]
    conflicts = [o for o in outcomes if o.endswith(":conflict")]
    assert len(oks) == 1 and len(conflicts) == 1  # 恰好一个成功、一个冲突
    po = svc.get_order("po1")
    assert po["version"] == 2
    # 成功的变更整体生效；失败的变更不留任何痕迹（不发生字段级混合）
    if oks == ["A:ok"]:
        assert po["amount"] == 1 and po["quantity"] == 10
    else:
        assert po["quantity"] == 99 and po["amount"] == 5000
    assert len(po["versions"]) == 2


def test_unknown_fields_rejected_not_silently_written(svc):
    place_order(svc)
    with pytest.raises(PreconditionFailedError) as ei:
        svc.change_order("po1", {"supplier_id": "s999"}, "篡改供应商", "buyer",
                         expected_version=1)
    assert ei.value.code == "UNKNOWN_FIELDS"
    assert svc.get_order("po1")["supplier_id"] == "s1"


def test_rollback_appends_version_with_historical_content(svc):
    place_order(svc)
    svc.change_order("po1", {"amount": 4500, "quantity": 8}, "改价改量", "buyer",
                     expected_version=1)
    svc.change_order("po1", {"amount": 4300, "note": "再降"}, "二次降价", "buyer",
                     expected_version=2)
    po = svc.get_order("po1")
    assert po["version"] == 3 and po["amount"] == 4300
    # 回滚到 v1
    svc.rollback_order("po1", target_version=1, reason="合同取消恢复原价",
                       actor="buyer", expected_version=3)
    po = svc.get_order("po1")
    assert po["version"] == 4
    assert po["amount"] == 5000 and po["quantity"] == 10
    assert po["item"] == "item" and po["note"] == "v1 note"
    assert "rollback to v1" in po["versions"][-1]["change_reason"]
    # 全部历史仍可查
    assert [v["version"] for v in po["versions"]] == [1, 2, 3, 4]


def test_rollback_then_behavior_matches_historical_version(svc):
    """回退后基于新版本继续变更，行为与当初基于 v1 完全一致（字段一致）。"""

    place_order(svc)
    svc.change_order("po1", {"amount": 1, "quantity": 1}, "极端改动", "buyer",
                     expected_version=1)
    svc.rollback_order("po1", 1, "撤回", "buyer", expected_version=2)
    po = svc.get_order("po1")
    assert po["amount"] == 5000 and po["quantity"] == 10
    # 当前版本号必须是新的 3，旧的乐观锁版本 2 已失效
    with pytest.raises(ConflictError):
        svc.change_order("po1", {"note": "x"}, "stale", "buyer", expected_version=2)
    svc.change_order("po1", {"note": "回滚后变更"}, "后续", "buyer", expected_version=3)
    po = svc.get_order("po1")
    assert po["version"] == 4 and po["amount"] == 5000 and po["quantity"] == 10


def test_rollback_to_missing_version_rejected(svc):
    place_order(svc)
    with pytest.raises(VersionNotFoundError):
        svc.rollback_order("po1", 9, "x", "buyer", expected_version=1)
    with pytest.raises(PreconditionFailedError) as ei:
        svc.rollback_order("po1", 1, "x", "buyer", expected_version=1)
    assert ei.value.code == "ROLLBACK_SAME_VERSION"


def test_change_requires_reason_and_correct_state(svc):
    place_order(svc)
    with pytest.raises(PreconditionFailedError):
        svc.change_order("po1", {"amount": 1}, "", "buyer", expected_version=1)
    # 完结后不可变更
    svc.complete_order("po1", "buyer")
    with pytest.raises(IllegalTransitionError):
        svc.change_order("po1", {"amount": 1}, "closed?", "buyer", expected_version=1)
