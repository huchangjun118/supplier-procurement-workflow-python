"""崩溃恢复：重放已提交事件，区分已提交结果与未完成流程。

语义：
- WAL 中带 commit 标记的事件 = 已提交结果，重启后全部保留；
- 没有 commit 的 prepare 行 = 未完成事务，Journal 重放时直接丢弃；
- 投影到非终态（如 QUOTING / AWARDED / APPROVING / APPROVED）的需求
  属于"停在中间"，可从最近一次已提交位置继续调用对应动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .journal import Event
from .models import RequisitionStatus

# 中断后可以继续推进的中间状态（ORDERED 已下单但未完结，仍需 complete_order）
RESUMABLE = frozenset({
    RequisitionStatus.SUBMITTED.value,
    RequisitionStatus.QUOTING.value,
    RequisitionStatus.AWARDED.value,
    RequisitionStatus.APPROVING.value,
    RequisitionStatus.APPROVED.value,
    RequisitionStatus.ORDERED.value,
})

# 已到达终态提交点（不会再推进）
COMMITTED_STATES = frozenset({
    RequisitionStatus.CLOSED.value,
    RequisitionStatus.CANCELLED.value,
    RequisitionStatus.REJECTED.value,
})


@dataclass(frozen=True)
class RecoveryReport:
    """重启恢复后的盘点结果。"""

    last_committed_seq: int
    event_count: int
    # 已走完关键提交点的实体
    committed_requisitions: tuple[str, ...] = field(default_factory=tuple)
    committed_orders: tuple[str, ...] = field(default_factory=tuple)
    # 停在中间状态、可继续推进的流程：(需求ID, 状态, 下一步建议)
    pending_requisitions: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    # 已审批但尚未下单的需求（最容易"停在中间"的位置）
    approved_not_ordered: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "last_committed_seq": self.last_committed_seq,
            "event_count": self.event_count,
            "committed_requisitions": list(self.committed_requisitions),
            "committed_orders": list(self.committed_orders),
            "pending_requisitions": [list(x) for x in self.pending_requisitions],
            "approved_not_ordered": list(self.approved_not_ordered),
        }


_NEXT_ACTION = {
    RequisitionStatus.DRAFT.value: "submit_requisition（草稿尚未提交）",
    RequisitionStatus.SUBMITTED.value: "open_quoting（开放报价）",
    RequisitionStatus.QUOTING.value: "供应商报价；完成后 close_quoting 裁决",
    RequisitionStatus.AWARDED.value: "start_approval（启动分级审批）",
    RequisitionStatus.APPROVING.value: "当前审批人 approve / reject_approval / transfer_approval",
    RequisitionStatus.APPROVED.value: "create_order（生成采购单）",
    RequisitionStatus.ORDERED.value: "complete_order（完结采购单）",
}


def replay(events: list[Event]) -> RecoveryReport:
    """基于已提交事件流，按聚合还原最终状态并生成恢复报告。

    这里不重建完整对象，只跟踪每个需求的最后状态/是否有采购单，
    与引擎投影解耦，恢复报告本身也是对事件链的独立审计。
    """

    last_status: dict[str, str] = {}
    created: set[str] = set()
    orders: set[str] = set()
    req_has_order: set[str] = set()

    for ev in events:
        if ev.kind == "ORDER_CREATED":
            orders.add(ev.aggregate_id)
            rid = ev.payload.get("requisition_id")
            if rid:
                req_has_order.add(rid)
                # 下单产生的 REQUISITION_ORDERED 状态事件挂在采购单聚合上，
                # 恢复盘点必须把需求状态一并推进
                last_status[rid] = RequisitionStatus.ORDERED.value
        if ev.aggregate_type != "requisition":
            continue
        rid = ev.aggregate_id
        if ev.kind == "REQUISITION_CREATED":
            created.add(rid)
            last_status[rid] = RequisitionStatus.DRAFT.value
        elif ev.kind in {
            "REQUISITION_SUBMITTED", "REQUISITION_QUOTING_OPENED",
            "REQUISITION_QUOTING_CLOSED", "APPROVAL_STARTED",
            "REQUISITION_APPROVED", "REQUISITION_REJECTED",
            "REQUISITION_ORDERED", "REQUISITION_CANCELLED", "REQUISITION_CLOSED",
        }:
            last_status[rid] = ev.payload.get("status", last_status.get(rid, ""))

    committed_reqs: list[str] = []
    pending: list[tuple[str, str, str]] = []
    approved_not_ordered: list[str] = []
    for rid in sorted(created):
        status = last_status.get(rid, RequisitionStatus.DRAFT.value)
        if status in COMMITTED_STATES:
            committed_reqs.append(rid)
        elif status in RESUMABLE:
            pending.append((rid, status, _NEXT_ACTION.get(status, "检查状态")))
            if status == RequisitionStatus.APPROVED.value and rid not in req_has_order:
                approved_not_ordered.append(rid)

    return RecoveryReport(
        last_committed_seq=events[-1].seq if events else 0,
        event_count=len(events),
        committed_requisitions=tuple(committed_reqs),
        committed_orders=tuple(sorted(orders)),
        pending_requisitions=tuple(pending),
        approved_not_ordered=tuple(approved_not_ordered),
    )


def is_resumable_status(status: str) -> bool:
    """该状态是否属于"中断后可从最近提交点继续"的中间状态。"""

    return status in RESUMABLE
