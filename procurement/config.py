"""工作流配置：分级审批规则与可调参数。"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ApprovalTier:
    """一级审批规则。

    审批门槛为 amount_from：采购金额 >= amount_from 的每一级都要审批（分级累加：
    金额 150_000 会同时命中 manager/director/vp 三个节点）。amount_to 仅用于
    说明该层级的区间上界，不参与命中判定。节点默认审批人为 default_approver（可转交）。
    """

    seq: int
    role: str
    amount_from: float
    amount_to: float | None
    default_approver: str

    def matches(self, amount: float) -> bool:
        return amount >= self.amount_from


def default_tiers() -> tuple[ApprovalTier, ...]:
    """内置三级分级审批：

    - 0  <= amount < 10_000   主管 manager
    - 10_000 <= amount < 100_000  总监 director
    - 100_000 <= amount          副总 vp
    命中的每一级都要审批（金额 150_000 需要 manager + director + vp 三节点）。
    """

    return (
        ApprovalTier(1, "manager", 0.0, 10_000.0, "alice"),
        ApprovalTier(2, "director", 10_000.0, 100_000.0, "bob"),
        ApprovalTier(3, "vp", 100_000.0, None, "carol"),
    )


@dataclass(frozen=True)
class WorkflowConfig:
    """全部可调流程参数。"""

    approval_tiers: tuple[ApprovalTier, ...] = default_tiers()
    # 报价提交后若未显式给出有效期，默认有效天数
    default_quote_valid_days: int = 30
    # open_quoting 未指定截止时间时的默认报价窗口（小时）
    default_quote_window_hours: int = 72
    # 裁决时过期报价（valid_until 早于裁决时刻）是否允许参与：默认不允许
    expired_quote_eligible: bool = False
    # 采购单允许变更的状态（其他状态下拒绝变更）
    order_mutable_status: tuple[str, ...] = ("APPROVED", "ORDERED")

    def tiers_for(self, amount: float) -> tuple[ApprovalTier, ...]:
        """返回某金额命中的全部审批层级（按 seq 升序）。"""

        return tuple(t for t in sorted(self.approval_tiers, key=lambda t: t.seq) if t.matches(amount))

    def with_tiers(self, tiers: tuple[ApprovalTier, ...]) -> "WorkflowConfig":
        return replace(self, approval_tiers=tiers)
