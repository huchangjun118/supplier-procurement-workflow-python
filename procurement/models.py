"""领域模型：采购需求、供应商报价、审批链、采购单版本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class RequisitionStatus(str, Enum):
    """采购需求状态机：

    DRAFT -> SUBMITTED -> QUOTING -> AWARDED -> APPROVING -> APPROVED
                                                                   \\
                                                                    -> ORDERED -> CLOSED
    任意活跃阶段 -> CANCELLED / REJECTED（终态）
    """

    DRAFT = "DRAFT"                       # 草稿，可编辑，尚未进入流程
    SUBMITTED = "SUBMITTED"               # 需求已提交，等待开放报价
    QUOTING = "QUOTING"                 # 报价中，供应商可并发提交报价
    AWARDED = "AWARDED"                 # 报价截止并已裁决出中标供应商
    APPROVING = "APPROVING"             # 分级审批进行中
    APPROVED = "APPROVED"               # 全部审批节点通过
    ORDERED = "ORDERED"                 # 采购单已生成
    CLOSED = "CLOSED"                   # 流程完结
    CANCELLED = "CANCELLED"             # 需求被取消（终态）
    REJECTED = "REJECTED"               # 审批被驳回（终态）

    @property
    def is_terminal(self) -> bool:
        return self in (RequisitionStatus.CANCELLED, RequisitionStatus.REJECTED, RequisitionStatus.CLOSED)


class SupplierStatus(str, Enum):
    """供应商准入状态。

    PENDING -> ADMITTED -> DISABLED
                ADMITTED -> REJECTED
    DISABLED -> ADMITTED（重新启用）
    """

    PENDING = "PENDING"
    ADMITTED = "ADMITTED"
    REJECTED = "REJECTED"
    DISABLED = "DISABLED"

    @property
    def can_participate(self) -> bool:
        """是否可以参与新的报价与采购单。"""
        return self is SupplierStatus.ADMITTED


class ApprovalNodeStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    TRANSFERRED = "TRANSFERRED"   # 原审批人已转交，节点本身仍需新审批人处理


@dataclass(frozen=True)
class ApprovalNode:
    """审批链上的一个节点（按金额与角色分级生成）。"""

    seq: int
    role: str
    amount_from: float
    amount_to: float | None
    approver: str
    status: ApprovalNodeStatus = ApprovalNodeStatus.PENDING
    decided_at: str | None = None
    comment: str = ""
    # 转交历史：[(from_user, to_user, reason, at)]
    transfers: tuple[tuple[str, str, str, str], ...] = ()


@dataclass
class Supplier:
    id: str
    name: str
    status: SupplierStatus
    reject_reason: str = ""          # 未通过准入 / 停用的原因，历史单据可查
    status_history: list[tuple[SupplierStatus, str, str]] = field(default_factory=list)


class QuoteStatus(str, Enum):
    SUBMITTED = "SUBMITTED"         # 已接收、参与裁决
    LATE = "LATE"                   # 裁决结束后才到达，仅留痕不参与裁决
    WINNING = "WINNING"             # 中标
    LOSING = "LOSING"               # 未中标（有效但未胜出）
    REJECTED = "REJECTED"           # 因供应商状态等原因被拒收


@dataclass(frozen=True)
class Quote:
    """供应商报价（不可变；重复报价被拒绝而非覆盖）。"""

    id: str
    requisition_id: str
    supplier_id: str
    amount: float
    valid_until: str          # ISO 时间字符串；报价有效期截止时刻
    submitted_at: str
    status: QuoteStatus
    note: str = ""


@dataclass(frozen=True)
class AwardDecision:
    """裁决结果及可解释依据。"""

    winning_quote_id: str
    winning_supplier_id: str
    amount: float
    reason: str                          # 人类可读的选择依据
    considered_quote_ids: tuple[str, ...]   # 参与裁决的全部报价
    rejected_late_quote_ids: tuple[str, ...] = ()
    decided_at: str = ""


@dataclass
class PurchaseOrder:
    """采购单（一次需求最多一个采购单；变更以版本形式保存）。"""

    id: str
    requisition_id: str
    supplier_id: str
    amount: float
    item: str
    quantity: int
    expected_delivery: str
    versions: list["OrderVersion"] = field(default_factory=list)
    current_version: int = 0

    def snapshot(self) -> dict:
        v = self.versions[self.current_version - 1]
        return {
            "id": self.id,
            "requisition_id": self.requisition_id,
            "supplier_id": self.supplier_id,
            "amount": v.amount,
            "item": v.item,
            "quantity": v.quantity,
            "expected_delivery": v.expected_delivery,
            "version": v.version,
            "created_at": v.created_at,
            "note": v.note,
        }


@dataclass(frozen=True)
class OrderVersion:
    """采购单的一个不可变历史版本。"""

    version: int
    amount: float
    item: str
    quantity: int
    expected_delivery: str
    note: str
    created_at: str
    change_reason: str = ""


@dataclass
class Requisition:
    """采购需求聚合根。"""

    id: str
    title: str
    requester: str
    estimated_amount: float
    status: RequisitionStatus = RequisitionStatus.DRAFT
    created_at: str = ""
    # 报价截止时间；裁决只能在此之后或手动 close_quoting 时发生
    quote_deadline: str | None = None
    quotes: dict[str, Quote] = field(default_factory=dict)
    award: AwardDecision | None = None
    approvals: list[ApprovalNode] = field(default_factory=list)
    purchase_order_id: str | None = None
    # 每次状态流转留痕：(from_status, to_status, action, actor, at, detail)
    history: list[tuple[str, str, str, str, str, str]] = field(default_factory=list)
