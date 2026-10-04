"""工作流引擎：纯领域逻辑，基于事件的确定性状态机。

命令 (command) 的工作方式：
1. 在工作副本状态上做全部前置校验（抛可区分的 WorkflowError 子类）；
2. 构造事件 (kind, payload) 并通过唯一的 _apply 路径作用到工作副本；
3. 返回事件列表。持久化/重放走同一个 _apply，因此"当时怎么执行，重启后就怎么恢复"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import WorkflowConfig
from .errors import (
    ConflictError,
    DuplicateQuoteError,
    DuplicateSubmitError,
    EntityNotFoundError,
    IllegalTransitionError,
    PreconditionFailedError,
    QuoteRejectedError,
    SupplierDisabledError,
    SupplierNotAdmittedError,
    SupplierNotFoundError,
    TransferError,
    UnauthorizedApprovalError,
    VersionNotFoundError,
)
from .models import (
    ApprovalNode,
    ApprovalNodeStatus,
    AwardDecision,
    OrderVersion,
    PurchaseOrder,
    Quote,
    QuoteStatus,
    Requisition,
    RequisitionStatus,
    Supplier,
    SupplierStatus,
)

EventItem = tuple[str, dict[str, Any]]

# 采购单可变更字段（全量校验，杜绝未知字段被静默写入）
ORDER_EDITABLE_FIELDS = frozenset({"amount", "item", "quantity", "expected_delivery", "note"})


class WorkflowEngine:
    def __init__(self, config: WorkflowConfig | None = None, clock: Callable[[], str] | None = None) -> None:
        self.config = config or WorkflowConfig()
        self._clock = clock

    def now(self) -> str:
        if self._clock is not None:
            return self._clock()
        return datetime.now(timezone.utc).isoformat()

    # ================================================================== #
    # 供应商准入
    # ================================================================== #
    def register_supplier(self, state: dict, supplier_id: str, name: str, actor: str) -> list[EventItem]:
        if supplier_id in state["suppliers"]:
            raise DuplicateSubmitError(f"供应商 {supplier_id} 已存在，不可重复注册")
        at = self.now()
        return self._fire(state, "supplier", supplier_id, "SUPPLIER_REGISTERED", {
            "name": name, "status": SupplierStatus.PENDING.value, "reason": "",
        }, actor, at)

    def admit_supplier(self, state: dict, supplier_id: str, actor: str) -> list[EventItem]:
        sup = self._supplier(state, supplier_id)
        if sup.status is SupplierStatus.ADMITTED:
            return []  # 重复准入：幂等无操作
        if sup.status is SupplierStatus.DISABLED:
            raise IllegalTransitionError(
                f"供应商 {supplier_id} 已停用，请使用启用(enable)流程", code="SUPPLIER_USE_ENABLE")
        at = self.now()
        return self._fire(state, "supplier", supplier_id, "SUPPLIER_ADMITTED",
                          {"status": SupplierStatus.ADMITTED.value, "reason": ""}, actor, at)

    def reject_supplier(self, state: dict, supplier_id: str, reason: str, actor: str) -> list[EventItem]:
        sup = self._supplier(state, supplier_id)
        if sup.status is SupplierStatus.REJECTED:
            return []
        if not reason:
            raise PreconditionFailedError("拒绝准入必须填写原因", code="REASON_REQUIRED")
        if sup.status is SupplierStatus.ADMITTED:
            raise IllegalTransitionError("已通过准入的供应商请走停用流程", code="SUPPLIER_ALREADY_ADMITTED")
        at = self.now()
        return self._fire(state, "supplier", supplier_id, "SUPPLIER_REJECTED",
                          {"status": SupplierStatus.REJECTED.value, "reason": reason}, actor, at)

    def disable_supplier(self, state: dict, supplier_id: str, reason: str, actor: str) -> list[EventItem]:
        sup = self._supplier(state, supplier_id)
        if sup.status is SupplierStatus.DISABLED:
            return []
        if not reason:
            raise PreconditionFailedError("停用必须填写原因", code="REASON_REQUIRED")
        if sup.status is not SupplierStatus.ADMITTED:
            raise IllegalTransitionError(
                f"供应商当前状态 {sup.status.value} 不可停用（仅 ADMITTED 可停用）")
        at = self.now()
        return self._fire(state, "supplier", supplier_id, "SUPPLIER_DISABLED",
                          {"status": SupplierStatus.DISABLED.value, "reason": reason}, actor, at)

    def enable_supplier(self, state: dict, supplier_id: str, actor: str) -> list[EventItem]:
        sup = self._supplier(state, supplier_id)
        if sup.status is SupplierStatus.ADMITTED:
            return []
        if sup.status is not SupplierStatus.DISABLED:
            raise IllegalTransitionError(
                f"供应商当前状态 {sup.status.value} 不可启用（仅 DISABLED 可重新启用）")
        at = self.now()
        return self._fire(state, "supplier", supplier_id, "SUPPLIER_ENABLED",
                          {"status": SupplierStatus.ADMITTED.value, "reason": ""}, actor, at)

    # ================================================================== #
    # 采购需求
    # ================================================================== #
    def create_requisition(self, state: dict, req_id: str, title: str, requester: str,
                           estimated_amount: float) -> list[EventItem]:
        if req_id in state["requisitions"]:
            raise DuplicateSubmitError(f"采购需求 {req_id} 已存在，不可重复创建")
        if estimated_amount < 0:
            raise PreconditionFailedError("预估金额不能为负", code="INVALID_AMOUNT")
        at = self.now()
        return self._fire(state, "requisition", req_id, "REQUISITION_CREATED", {
            "title": title, "requester": requester, "estimated_amount": estimated_amount,
            "status": RequisitionStatus.DRAFT.value,
        }, requester, at)

    def submit_requisition(self, state: dict, req_id: str, actor: str) -> list[EventItem]:
        req = self._requisition(state, req_id)
        if req.status is RequisitionStatus.SUBMITTED:
            return []  # 重复提交：幂等
        self._require_status(req, RequisitionStatus.DRAFT, "提交需求")
        at = self.now()
        return self._fire(state, "requisition", req_id, "REQUISITION_SUBMITTED",
                          {"status": RequisitionStatus.SUBMITTED.value}, actor, at)

    def open_quoting(self, state: dict, req_id: str, actor: str, deadline: str | None = None) -> list[EventItem]:
        req = self._requisition(state, req_id)
        if req.status is RequisitionStatus.QUOTING:
            return []
        self._require_status(req, RequisitionStatus.SUBMITTED, "开放报价")
        at = self.now()
        if deadline is None:
            dl = (datetime.fromisoformat(at) + timedelta(hours=self.config.default_quote_window_hours)).isoformat()
        else:
            dl = deadline
            if datetime.fromisoformat(dl) <= datetime.fromisoformat(at):
                raise PreconditionFailedError("报价截止时间必须晚于当前时间", code="BAD_DEADLINE")
        return self._fire(state, "requisition", req_id, "REQUISITION_QUOTING_OPENED", {
            "status": RequisitionStatus.QUOTING.value, "quote_deadline": dl,
        }, actor, at)

    def close_quoting(self, state: dict, req_id: str, actor: str) -> list[EventItem]:
        req = self._requisition(state, req_id)
        self._require_status(req, RequisitionStatus.QUOTING, "裁决报价")
        at = self.now()
        now_dt = datetime.fromisoformat(at)
        eligible: list[Quote] = []
        expired: list[str] = []
        for q in req.quotes.values():
            if q.status is not QuoteStatus.SUBMITTED:
                continue
            if not self.config.expired_quote_eligible and datetime.fromisoformat(q.valid_until) < now_dt:
                expired.append(q.id)
                continue
            eligible.append(q)
        if not eligible:
            raise PreconditionFailedError(
                f"需求 {req_id} 没有有效报价可裁决"
                + (f"；{len(expired)} 条报价已过有效期" if expired else ""),
                code="NO_ELIGIBLE_QUOTE")
        # 确定性裁决：金额升序 -> 提交时间升序 -> 供应商 ID 升序
        eligible.sort(key=lambda q: (q.amount, q.submitted_at, q.supplier_id))
        winner = eligible[0]
        considered = tuple(q.id for q in sorted(req.quotes.values(), key=lambda q: q.submitted_at))
        reason = self._award_reason(req.quotes, winner, expired)
        decision = AwardDecision(
            winning_quote_id=winner.id,
            winning_supplier_id=winner.supplier_id,
            amount=winner.amount,
            reason=reason,
            considered_quote_ids=considered,
            rejected_late_quote_ids=tuple(
                q.id for q in req.quotes.values() if q.status is QuoteStatus.LATE),
            decided_at=at,
        )
        items: list[EventItem] = []
        # 各报价置为中标/落标/过期拒收
        for q in req.quotes.values():
            if q.status is QuoteStatus.SUBMITTED:
                new_status = QuoteStatus.WINNING if q.id == winner.id else (
                    QuoteStatus.REJECTED if q.id in expired else QuoteStatus.LOSING)
                items.extend(self._fire(state, "requisition", req_id, "QUOTE_DECIDED", {
                    "quote_id": q.id, "status": new_status.value,
                }, actor, at))
        items.extend(self._fire(state, "requisition", req_id, "REQUISITION_QUOTING_CLOSED", {
            "status": RequisitionStatus.AWARDED.value,
            "award": {
                "winning_quote_id": decision.winning_quote_id,
                "winning_supplier_id": decision.winning_supplier_id,
                "amount": decision.amount,
                "reason": decision.reason,
                "considered_quote_ids": list(decision.considered_quote_ids),
                "rejected_late_quote_ids": list(decision.rejected_late_quote_ids),
                "decided_at": at,
            },
        }, actor, at))
        return items

    def cancel_requisition(self, state: dict, req_id: str, actor: str, reason: str = "") -> list[EventItem]:
        req = self._requisition(state, req_id)
        if req.status is RequisitionStatus.CANCELLED:
            return []
        if req.status.is_terminal:
            raise IllegalTransitionError(f"需求处于终态 {req.status.value}，不可取消")
        if req.status is RequisitionStatus.ORDERED:
            raise IllegalTransitionError("已生成采购单，不可取消需求（请走采购单完结/变更流程）")
        at = self.now()
        return self._fire(state, "requisition", req_id, "REQUISITION_CANCELLED", {
            "status": RequisitionStatus.CANCELLED.value, "detail": reason,
        }, actor, at)

    # ================================================================== #
    # 供应商报价（并发安全：服务层串行化进入引擎）
    # ================================================================== #
    def submit_quote(self, state: dict, quote_id: str, req_id: str, supplier_id: str,
                     amount: float, valid_until: str, actor: str,
                     accept_late: bool = False) -> list[EventItem]:
        req = self._requisition(state, req_id)
        sup = self._supplier(state, supplier_id)
        # 供应商状态门禁：未准入/停用 不能参与新报价；历史报价不受影响
        self._require_admitted(sup)
        if amount <= 0:
            raise PreconditionFailedError("报价金额必须为正数", code="INVALID_AMOUNT")
        at = self.now()
        # 裁决已结束：迟到报价只留痕，不改变结果
        if req.status not in (RequisitionStatus.QUOTING, RequisitionStatus.AWARDED,
                              RequisitionStatus.APPROVING, RequisitionStatus.APPROVED,
                              RequisitionStatus.ORDERED):
            raise QuoteRejectedError(
                f"需求状态 {req.status.value} 不接收报价", code="QUOTE_WRONG_PHASE")
        if req.award is not None:
            return self._fire_late(state, req_id, quote_id, supplier_id, amount,
                                   valid_until, at, actor)
        # 仍在报价窗口：检查截止时间
        if req.quote_deadline is not None and datetime.fromisoformat(at) > datetime.fromisoformat(req.quote_deadline):
            # 显式迟到 -> 拒绝；与截止动作并发竞争落败 -> 记为 LATE 留痕
            if not accept_late:
                raise QuoteRejectedError("报价已超过截止时间", code="QUOTE_DEADLINE_PASSED")
            return self._fire_late(state, req_id, quote_id, supplier_id, amount,
                                   valid_until, at, actor)
        if datetime.fromisoformat(valid_until) <= datetime.fromisoformat(at):
            raise QuoteRejectedError("报价有效期必须晚于提交时间", code="QUOTE_VALIDITY_EXPIRED")
        if any(q.supplier_id == supplier_id and q.status is QuoteStatus.SUBMITTED
               for q in req.quotes.values()):
            raise DuplicateQuoteError(f"供应商 {supplier_id} 已对需求 {req_id} 报价，重复报价被拒绝")
        if quote_id in req.quotes:
            raise DuplicateSubmitError(f"报价单 {quote_id} 编号重复")
        return self._fire(state, "requisition", req_id, "QUOTE_SUBMITTED", {
            "quote_id": quote_id, "supplier_id": supplier_id, "amount": amount,
            "valid_until": valid_until, "submitted_at": at,
            "status": QuoteStatus.SUBMITTED.value,
        }, actor, at)

    def _fire_late(self, state: dict, req_id: str, quote_id: str, supplier_id: str,
                   amount: float, valid_until: str, at: str, actor: str) -> list[EventItem]:
        if any(q.supplier_id == supplier_id for q in state["requisitions"][req_id].quotes.values()):
            raise DuplicateQuoteError(
                f"供应商 {supplier_id} 已有报价，迟到的重复报价被拒绝")
        if quote_id in state["requisitions"][req_id].quotes:
            raise DuplicateSubmitError(f"报价单 {quote_id} 编号重复")
        return self._fire(state, "requisition", req_id, "QUOTE_LATE", {
            "quote_id": quote_id, "supplier_id": supplier_id, "amount": amount,
            "valid_until": valid_until, "submitted_at": at,
            "status": QuoteStatus.LATE.value,
        }, actor, at)

    # ================================================================== #
    # 分级审批
    # ================================================================== #
    def start_approval(self, state: dict, req_id: str, actor: str) -> list[EventItem]:
        req = self._requisition(state, req_id)
        if req.status is RequisitionStatus.APPROVING:
            return []
        self._require_status(req, RequisitionStatus.AWARDED, "启动审批")
        if req.award is None:
            raise PreconditionFailedError("缺少裁决结果，无法启动审批", code="NO_AWARD")
        tiers = self.config.tiers_for(req.award.amount)
        if not tiers:
            raise PreconditionFailedError(
                f"金额 {req.award.amount} 没有匹配的审批层级，请检查审批配置", code="NO_APPROVAL_TIER")
        at = self.now()
        nodes = [{
            "seq": t.seq, "role": t.role, "approver": t.default_approver,
            "amount_from": t.amount_from, "amount_to": t.amount_to,
        } for t in tiers]
        return self._fire(state, "requisition", req_id, "APPROVAL_STARTED", {
            "status": RequisitionStatus.APPROVING.value, "nodes": nodes,
        }, actor, at)

    def approve(self, state: dict, req_id: str, approver: str, comment: str = "") -> list[EventItem]:
        req = self._requisition(state, req_id)
        self._require_status(req, RequisitionStatus.APPROVING, "审批")
        node = self._pending_node(req)
        if node.approver != approver:
            self._raise_unauthorized(req, node, approver)
        at = self.now()
        items = self._fire(state, "requisition", req_id, "NODE_APPROVED", {
            "seq": node.seq, "status": ApprovalNodeStatus.APPROVED.value,
            "decided_at": at, "comment": comment,
        }, approver, at)
        # 工作副本已随 _fire 更新：若没有剩余 PENDING 节点，则整条审批链通过
        if all(n.status is ApprovalNodeStatus.APPROVED for n in req.approvals):
            items.extend(self._fire(state, "requisition", req_id, "REQUISITION_APPROVED", {
                "status": RequisitionStatus.APPROVED.value,
            }, approver, at))
        return items

    def reject_approval(self, state: dict, req_id: str, approver: str, comment: str = "") -> list[EventItem]:
        req = self._requisition(state, req_id)
        self._require_status(req, RequisitionStatus.APPROVING, "驳回审批")
        node = self._pending_node(req)
        if node.approver != approver:
            self._raise_unauthorized(req, node, approver)
        if not comment:
            raise PreconditionFailedError("驳回必须填写意见", code="COMMENT_REQUIRED")
        at = self.now()
        items = self._fire(state, "requisition", req_id, "NODE_REJECTED", {
            "seq": node.seq, "status": ApprovalNodeStatus.REJECTED.value,
            "decided_at": at, "comment": comment,
        }, approver, at)
        items.extend(self._fire(state, "requisition", req_id, "REQUISITION_REJECTED", {
            "status": RequisitionStatus.REJECTED.value, "detail": comment,
        }, approver, at))
        return items

    def transfer_approval(self, state: dict, req_id: str, from_user: str, to_user: str,
                          to_role: str, reason: str) -> list[EventItem]:
        req = self._requisition(state, req_id)
        self._require_status(req, RequisitionStatus.APPROVING, "转交审批")
        node = self._pending_node(req)
        if node.approver != from_user:
            raise UnauthorizedApprovalError(
                f"只有当前审批人 {node.approver} 可以转交节点 {node.seq}，{from_user} 无权操作")
        if not reason:
            raise PreconditionFailedError("转交必须填写原因（如离职/变更）", code="REASON_REQUIRED")
        if to_user == from_user:
            raise TransferError("不能转交给自己")
        # 越权防护：接收人角色必须不低于本节点层级
        # （门槛金额越高权限越大：vp 可接管 manager 的节点，反之不可）
        role_tier = next((t for t in self.config.approval_tiers if t.role == to_role), None)
        if role_tier is None or role_tier.amount_from < node.amount_from:
            raise TransferError(
                f"角色 {to_role} 无权承接层级 {node.seq}（角色 {node.role}）的审批，转交被拒绝")
        at = self.now()
        return self._fire(state, "requisition", req_id, "NODE_TRANSFERRED", {
            "seq": node.seq, "from_user": from_user, "to_user": to_user,
            "to_role": to_role, "reason": reason, "at": at,
        }, from_user, at)

    # ================================================================== #
    # 采购单：创建 / 版本化变更 / 回滚 / 完结
    # ================================================================== #
    def create_order(self, state: dict, order_id: str, req_id: str, amount: float, item: str,
                     quantity: int, expected_delivery: str, note: str, actor: str,
                     expected_version: int = 0) -> list[EventItem]:
        req = self._requisition(state, req_id)
        if order_id in state["orders"]:
            raise DuplicateSubmitError(f"采购单 {order_id} 已存在", )
        if req.purchase_order_id is not None:
            raise IllegalTransitionError(
                f"需求 {req_id} 已生成采购单 {req.purchase_order_id}，禁止重复下单",
                code="ALREADY_ORDERED")
        self._require_status(req, RequisitionStatus.APPROVED, "生成采购单")
        if expected_version != 0:
            raise ConflictError("新建采购单 expected_version 必须为 0")
        if amount <= 0 or quantity <= 0:
            raise PreconditionFailedError("采购单金额与数量必须为正数", code="INVALID_AMOUNT")
        sup = self._supplier(state, req.award.winning_supplier_id)
        self._require_admitted(sup)  # 新单据仍受供应商当前状态约束
        at = self.now()
        version = {
            "version": 1, "amount": amount, "item": item, "quantity": quantity,
            "expected_delivery": expected_delivery, "note": note,
            "created_at": at, "change_reason": "initial creation",
        }
        items = self._fire(state, "purchase_order", order_id, "ORDER_CREATED", {
            "requisition_id": req_id, "supplier_id": sup.id, "current_version": 1, "version": version,
        }, actor, at)
        items.extend(self._fire(state, "requisition", req_id, "REQUISITION_ORDERED", {
            "status": RequisitionStatus.ORDERED.value, "purchase_order_id": order_id,
        }, actor, at))
        return items

    def change_order(self, state: dict, order_id: str, changes: dict[str, Any], reason: str,
                     actor: str, expected_version: int) -> list[EventItem]:
        order = self._order(state, order_id)
        req = self._requisition(state, order.requisition_id)
        if req.status is not RequisitionStatus.ORDERED:
            raise IllegalTransitionError(
                f"采购单当前需求状态为 {req.status.value}，不可变更（仅 ORDERED 可变更）")
        if not reason:
            raise PreconditionFailedError("变更必须填写原因", code="REASON_REQUIRED")
        unknown = set(changes) - ORDER_EDITABLE_FIELDS
        if unknown:
            raise PreconditionFailedError(f"存在不可变更字段：{sorted(unknown)}", code="UNKNOWN_FIELDS")
        if not changes:
            raise PreconditionFailedError("变更内容为空", code="EMPTY_CHANGE")
        self._check_version(order, expected_version)
        cur = order.versions[order.current_version - 1]
        at = self.now()
        new_version = {
            "version": order.current_version + 1,
            "amount": float(changes.get("amount", cur.amount)),
            "item": changes.get("item", cur.item),
            "quantity": int(changes.get("quantity", cur.quantity)),
            "expected_delivery": changes.get("expected_delivery", cur.expected_delivery),
            "note": changes.get("note", cur.note),
            "created_at": at, "change_reason": reason,
        }
        if new_version["amount"] <= 0 or new_version["quantity"] <= 0:
            raise PreconditionFailedError("变更后金额与数量必须为正数", code="INVALID_AMOUNT")
        return self._fire(state, "purchase_order", order_id, "ORDER_VERSION_ADDED", {
            "current_version": new_version["version"], "version": new_version,
        }, actor, at)

    def rollback_order(self, state: dict, order_id: str, target_version: int, reason: str,
                       actor: str, expected_version: int) -> list[EventItem]:
        order = self._order(state, order_id)
        req = self._requisition(state, order.requisition_id)
        if req.status is not RequisitionStatus.ORDERED:
            raise IllegalTransitionError(
                f"采购单当前需求状态为 {req.status.value}，不可回滚（仅 ORDERED 可回滚）")
        if target_version < 1 or target_version > order.current_version:
            raise VersionNotFoundError(
                f"版本 {target_version} 不存在（当前共 {order.current_version} 个版本）")
        if target_version == order.current_version:
            raise PreconditionFailedError("目标版本即当前版本，无需回滚", code="ROLLBACK_SAME_VERSION")
        self._check_version(order, expected_version)
        target = order.versions[target_version - 1]
        at = self.now()
        # 回滚 = 以历史版本内容追加一个新版本；内容与当时完全一致
        new_version = {
            "version": order.current_version + 1,
            "amount": target.amount, "item": target.item, "quantity": target.quantity,
            "expected_delivery": target.expected_delivery, "note": target.note,
            "created_at": at,
            "change_reason": f"rollback to v{target_version}: {reason}",
        }
        return self._fire(state, "purchase_order", order_id, "ORDER_VERSION_ADDED", {
            "current_version": new_version["version"], "version": new_version,
            "rolled_back_from": target_version,
        }, actor, at)

    def complete_order(self, state: dict, order_id: str, actor: str) -> list[EventItem]:
        order = self._order(state, order_id)
        req = self._requisition(state, order.requisition_id)
        if req.status is RequisitionStatus.CLOSED:
            return []
        self._require_status(req, RequisitionStatus.ORDERED, "完结采购单")
        at = self.now()
        return self._fire(state, "requisition", req.id, "REQUISITION_CLOSED", {
            "status": RequisitionStatus.CLOSED.value, "purchase_order_id": order.id,
        }, actor, at)

    # ================================================================== #
    # 事件投影：执行与重放的唯一变更路径
    # ================================================================== #
    def apply(self, state: dict, kind: str, payload: dict[str, Any], actor: str, at: str) -> None:
        """把一条已提交事件作用到状态上（幂等：调用方保证事件不重复）。"""

        if kind.startswith("SUPPLIER_"):
            self._apply_supplier(state, kind, payload, actor, at)
        elif kind.startswith("ORDER_"):
            self._apply_order(state, kind, payload, actor, at)
        else:
            self._apply_requisition(state, kind, payload, actor, at)

    def new_state(self) -> dict:
        return {"requisitions": {}, "orders": {}, "suppliers": {}}

    # ------------------------------------------------------------------ #
    # 供应商投影
    # ------------------------------------------------------------------ #
    def _apply_supplier(self, state: dict, kind: str, p: dict, actor: str, at: str) -> None:
        sid = p["_id"]
        if kind == "SUPPLIER_REGISTERED":
            sup = Supplier(id=sid, name=p["name"], status=SupplierStatus.PENDING)
            sup.status_history.append((SupplierStatus.PENDING.value, "registered", at))
            state["suppliers"][sid] = sup
            return
        sup = state["suppliers"][sid]
        new_status = SupplierStatus(p["status"])
        sup.status_history.append((new_status.value, p.get("reason", ""), at))
        sup.status = new_status
        sup.reject_reason = p.get("reason", "") if new_status in (
            SupplierStatus.REJECTED, SupplierStatus.DISABLED) else ""

    # ------------------------------------------------------------------ #
    # 采购单投影
    # ------------------------------------------------------------------ #
    def _apply_order(self, state: dict, kind: str, p: dict, actor: str, at: str) -> None:
        oid = p["_id"]
        if kind == "ORDER_CREATED":
            order = PurchaseOrder(
                id=oid, requisition_id=p["requisition_id"], supplier_id=p["supplier_id"],
                amount=p["version"]["amount"], item=p["version"]["item"],
                quantity=p["version"]["quantity"],
                expected_delivery=p["version"]["expected_delivery"])
            order.versions.append(self._version_from(p["version"]))
            order.current_version = 1
            state["orders"][oid] = order
            return
        order = state["orders"][oid]
        if kind == "ORDER_VERSION_ADDED":
            order.versions.append(self._version_from(p["version"]))
            order.current_version = p["current_version"]
            v = order.versions[-1]
            order.amount, order.item = v.amount, v.item
            order.quantity, order.expected_delivery = v.quantity, v.expected_delivery

    @staticmethod
    def _version_from(p: dict) -> OrderVersion:
        return OrderVersion(
            version=p["version"], amount=p["amount"], item=p["item"], quantity=p["quantity"],
            expected_delivery=p["expected_delivery"], note=p["note"],
            created_at=p["created_at"], change_reason=p.get("change_reason", ""))

    # ------------------------------------------------------------------ #
    # 需求投影
    # ------------------------------------------------------------------ #
    def _apply_requisition(self, state: dict, kind: str, p: dict, actor: str, at: str) -> None:
        rid = p["_id"]
        if kind == "REQUISITION_CREATED":
            req = Requisition(
                id=rid, title=p["title"], requester=p["requester"],
                estimated_amount=p["estimated_amount"],
                status=RequisitionStatus(p["status"]), created_at=at)
            state["requisitions"][rid] = req
            req.history.append(("-", req.status.value, "create", actor, at, ""))
            return
        req = state["requisitions"][rid]
        old_status = req.status.value

        if kind == "REQUISITION_SUBMITTED":
            req.status = RequisitionStatus.SUBMITTED
        elif kind == "REQUISITION_QUOTING_OPENED":
            req.status = RequisitionStatus.QUOTING
            req.quote_deadline = p["quote_deadline"]
        elif kind == "QUOTE_SUBMITTED":
            req.quotes[p["quote_id"]] = Quote(
                id=p["quote_id"], requisition_id=rid, supplier_id=p["supplier_id"],
                amount=p["amount"], valid_until=p["valid_until"],
                submitted_at=p["submitted_at"], status=QuoteStatus(p["status"]))
        elif kind == "QUOTE_LATE":
            req.quotes[p["quote_id"]] = Quote(
                id=p["quote_id"], requisition_id=rid, supplier_id=p["supplier_id"],
                amount=p["amount"], valid_until=p["valid_until"],
                submitted_at=p["submitted_at"], status=QuoteStatus.LATE)
            # 裁决已结束：迟到报价只追加到裁决留痕，不改变中标结果
            if req.award is not None and p["quote_id"] not in req.award.rejected_late_quote_ids:
                req.award = AwardDecision(
                    winning_quote_id=req.award.winning_quote_id,
                    winning_supplier_id=req.award.winning_supplier_id,
                    amount=req.award.amount, reason=req.award.reason,
                    considered_quote_ids=req.award.considered_quote_ids,
                    rejected_late_quote_ids=req.award.rejected_late_quote_ids + (p["quote_id"],),
                    decided_at=req.award.decided_at)
        elif kind == "QUOTE_DECIDED":
            q = req.quotes[p["quote_id"]]
            req.quotes[p["quote_id"]] = Quote(
                id=q.id, requisition_id=q.requisition_id, supplier_id=q.supplier_id,
                amount=q.amount, valid_until=q.valid_until, submitted_at=q.submitted_at,
                status=QuoteStatus(p["status"]), note=q.note)
        elif kind == "REQUISITION_QUOTING_CLOSED":
            a = p["award"]
            req.award = AwardDecision(
                winning_quote_id=a["winning_quote_id"], winning_supplier_id=a["winning_supplier_id"],
                amount=a["amount"], reason=a["reason"],
                considered_quote_ids=tuple(a["considered_quote_ids"]),
                rejected_late_quote_ids=tuple(a["rejected_late_quote_ids"]),
                decided_at=a["decided_at"])
            req.status = RequisitionStatus.AWARDED
        elif kind == "APPROVAL_STARTED":
            req.approvals = [
                ApprovalNode(seq=n["seq"], role=n["role"], amount_from=n["amount_from"],
                             amount_to=n["amount_to"], approver=n["approver"])
                for n in p["nodes"]]
            req.status = RequisitionStatus.APPROVING
        elif kind == "NODE_APPROVED":
            n = self._node(req, p["seq"])
            idx = req.approvals.index(n)
            req.approvals[idx] = ApprovalNode(
                seq=n.seq, role=n.role, amount_from=n.amount_from, amount_to=n.amount_to,
                approver=n.approver, status=ApprovalNodeStatus.APPROVED,
                decided_at=p["decided_at"], comment=p["comment"], transfers=n.transfers)
        elif kind == "NODE_REJECTED":
            n = self._node(req, p["seq"])
            idx = req.approvals.index(n)
            req.approvals[idx] = ApprovalNode(
                seq=n.seq, role=n.role, amount_from=n.amount_from, amount_to=n.amount_to,
                approver=n.approver, status=ApprovalNodeStatus.REJECTED,
                decided_at=p["decided_at"], comment=p["comment"], transfers=n.transfers)
        elif kind == "NODE_TRANSFERRED":
            n = self._node(req, p["seq"])
            idx = req.approvals.index(n)
            req.approvals[idx] = ApprovalNode(
                seq=n.seq, role=p["to_role"], amount_from=n.amount_from, amount_to=n.amount_to,
                approver=p["to_user"], status=ApprovalNodeStatus.PENDING,
                decided_at=None, comment="",
                transfers=n.transfers + ((p["from_user"], p["to_user"], p["reason"], p["at"]),))
        elif kind == "REQUISITION_APPROVED":
            req.status = RequisitionStatus.APPROVED
        elif kind == "REQUISITION_REJECTED":
            req.status = RequisitionStatus.REJECTED
        elif kind == "REQUISITION_ORDERED":
            req.status = RequisitionStatus.ORDERED
            req.purchase_order_id = p["purchase_order_id"]
        elif kind == "REQUISITION_CANCELLED":
            req.status = RequisitionStatus.CANCELLED
        elif kind == "REQUISITION_CLOSED":
            req.status = RequisitionStatus.CLOSED
        else:
            raise PreconditionFailedError(f"未知事件类型 {kind}", code="UNKNOWN_EVENT")

        if req.status.value != old_status:
            req.history.append((old_status, req.status.value, kind.lower(), actor, at, p.get("detail", "")))

    # ================================================================== #
    # 辅助方法
    # ================================================================== #
    def _fire(self, state: dict, aggregate_type: str, aggregate_id: str, kind: str,
              payload: dict[str, Any], actor: str, at: str) -> list[EventItem]:
        payload = dict(payload)
        payload["_id"] = aggregate_id
        payload["_actor"] = actor
        payload["_at"] = at
        self.apply(state, kind, payload, actor, at)
        return [(kind, payload)]

    @staticmethod
    def _requisition(state: dict, req_id: str) -> Requisition:
        req = state["requisitions"].get(req_id)
        if req is None:
            raise EntityNotFoundError(f"采购需求 {req_id} 不存在", code="REQUISITION_NOT_FOUND")
        return req

    @staticmethod
    def _order(state: dict, order_id: str) -> PurchaseOrder:
        order = state["orders"].get(order_id)
        if order is None:
            raise EntityNotFoundError(f"采购单 {order_id} 不存在", code="ORDER_NOT_FOUND")
        return order

    @staticmethod
    def _supplier(state: dict, supplier_id: str) -> Supplier:
        sup = state["suppliers"].get(supplier_id)
        if sup is None:
            raise SupplierNotFoundError(f"供应商 {supplier_id} 不存在")
        return sup

    @staticmethod
    def _require_admitted(sup: Supplier) -> None:
        if sup.status is SupplierStatus.ADMITTED:
            return
        if sup.status is SupplierStatus.DISABLED:
            raise SupplierDisabledError(
                f"供应商 {sup.id} 已停用：{sup.reject_reason or '无记录原因'}；不可参与新报价/采购单")
        raise SupplierNotAdmittedError(
            f"供应商 {sup.id} 未通过准入（状态 {sup.status.value}）：{sup.reject_reason or '待审核'}")

    @staticmethod
    def _require_status(req: Requisition, expected: RequisitionStatus, action: str) -> None:
        if req.status is not expected:
            raise IllegalTransitionError(
                f"需求 {req.id} 当前状态 {req.status.value}，不能执行【{action}】"
                f"（要求 {expected.value}）")

    @staticmethod
    def _pending_node(req: Requisition) -> ApprovalNode:
        for n in req.approvals:
            if n.status is ApprovalNodeStatus.PENDING:
                return n
        raise PreconditionFailedError("没有待审批节点", code="NO_PENDING_NODE")

    @staticmethod
    def _node(req: Requisition, seq: int) -> ApprovalNode:
        for n in req.approvals:
            if n.seq == seq:
                return n
        raise PreconditionFailedError(f"审批节点 {seq} 不存在", code="NODE_NOT_FOUND")

    def _raise_unauthorized(self, req: Requisition, node: ApprovalNode, approver: str) -> None:
        transferred = [t for n in req.approvals for t in n.transfers if n.seq == node.seq]
        hint = ""
        if transferred:
            hint = f"；该节点已由 {transferred[-1][0]} 转交给 {node.approver}"
        raise UnauthorizedApprovalError(
            f"{approver} 无权审批需求 {req.id} 的第 {node.seq} 级节点"
            f"（要求角色 {node.role} / 审批人 {node.approver}）{hint}")

    @staticmethod
    def _check_version(order: PurchaseOrder, expected_version: int) -> None:
        if expected_version != order.current_version:
            raise ConflictError(
                f"采购单 {order.id} 并发冲突：提交基于 v{expected_version}，"
                f"当前已是 v{order.current_version}，请基于最新版本重试")

    @staticmethod
    def _award_reason(quotes: dict[str, Quote], winner: Quote, expired: list[str]) -> str:
        lines = [
            f"中标报价 {winner.id}（供应商 {winner.supplier_id}，金额 {winner.amount}）。",
            "裁决规则：在全部有效（未过期、按时提交）报价中取最低金额；"
            "金额相同则先提交者胜出，再相同则按供应商编号排序。",
        ]
        rows = sorted(quotes.values(), key=lambda q: (q.amount, q.submitted_at, q.supplier_id))
        for q in rows:
            tag = "中标" if q.id == winner.id else (
                "已过期被排除" if q.id in expired else (
                    "迟到未参与" if q.status is QuoteStatus.LATE else "有效未中标"))
            lines.append(f"  - {q.id}: 金额={q.amount}, 提交于={q.submitted_at}, "
                         f"有效期至={q.valid_until} -> {tag}")
        return "\n".join(lines)
