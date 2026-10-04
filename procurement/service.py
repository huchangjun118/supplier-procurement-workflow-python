"""应用服务：事务封装、幂等控制、快照投影与查询。

职责边界：
- Journal 负责"事件原子提交 + 崩溃恢复"；
- WorkflowEngine 负责"状态机 + 裁决 + 审批"纯领域逻辑；
- 本类负责把两者串起来：串行事务、工作副本、幂等键、对外只读视图。
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from .config import WorkflowConfig
from .engine import WorkflowEngine
from .errors import EntityNotFoundError
from .journal import Event, Journal
from .models import (
    ApprovalNode,
    PurchaseOrder,
    Quote,
    Requisition,
    Supplier,
)


class ProcurementService:
    def __init__(self, journal: Journal | None = None, config: WorkflowConfig | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.journal = journal or Journal()
        self.config = config or WorkflowConfig()
        self.engine = WorkflowEngine(self.config, clock)
        self._state: dict[str, Any] = {"requisitions": {}, "orders": {}, "suppliers": {}}
        self._idem: dict[str, dict[str, Any]] = {}
        self._rebuild()

    # ================================================================== #
    # 内部：重放 / 事务
    # ================================================================== #
    def _rebuild(self) -> None:
        """从 journal 重放全部已提交事件，重建内存投影（重启恢复入口）。"""

        state = self.engine.new_state()
        idem: dict[str, dict[str, Any]] = {}
        for ev in self.journal.all_events():
            self.engine.apply(state, ev.kind, ev.payload, ev.actor, ev.at)
            if ev.idem_key and ev.idem_key not in idem:
                # 幂等记录的返回结果从最终投影即时生成（见 _result_for）
                idem[ev.idem_key] = {
                    "aggregate_type": ev.aggregate_type,
                    "aggregate_id": ev.aggregate_id,
                    "first_seq": ev.seq,
                }
        self._state = state
        self._idem = idem

    def _run(self, aggregate_type: str, aggregate_id: str,
             command: Callable[[dict[str, Any]], list], idem_key: str | None = None,
             result_id: tuple[str, str] | None = None,
             version_baseline: tuple[str, int] | None = None) -> dict[str, Any]:
        """通用事务模板。

        1. 幂等键命中 -> 直接回放首次结果，不进入领域逻辑；
        2. 在工作副本（深拷贝）上执行命令，任何异常都不会触及正式状态；
        3. 事件经 Journal 原子提交成功后，才用工作副本替换正式状态；
        4. version_baseline=(聚合类型, 期望版本)：在事务临界区入口校验版本。
           并发请求即使同时到达，进入临界区后会看到对方已提交的新版本，
           从而收到 VERSION_CONFLICT，杜绝字段级混合。
        """

        if idem_key is not None and idem_key in self._idem:
            rec = self._idem[idem_key]
            return self._idem_result(rec)

        def tx(appender) -> list[Event]:
            # 在临界区内、基于最新已提交状态校验乐观锁基线
            if version_baseline is not None:
                vt, expected = version_baseline
                current = self._current_version(vt, aggregate_id)
                if current != expected:
                    from .errors import ConflictError
                    raise ConflictError(
                        f"聚合 {vt}:{aggregate_id} 并发冲突：提交基于 v{expected}，"
                        f"当前已是 v{current}，请基于最新版本重试")
            scratch = copy.deepcopy(self._state)
            items = command(scratch)
            emitted: list[Event] = []
            for i, (kind, payload) in enumerate(items):
                key = idem_key if i == 0 and idem_key is not None else None
                emitted.append(appender.emit(
                    aggregate_type, payload.get("_id", aggregate_id), kind, payload,
                    payload.get("_actor", "system"), payload.get("_at", self.engine.now()),
                    idem_key=key))
            self._scratch_result = scratch
            return emitted

        events = self.journal.transactionally(tx)
        # 提交成功后切换投影
        self._state = self._scratch_result
        if idem_key is not None and events:
            self._idem[idem_key] = {
                "aggregate_type": events[0].aggregate_type,
                "aggregate_id": events[0].payload.get("_id", aggregate_id),
                "first_seq": events[0].seq,
            }
        return {"status": "ok", "events": len(events),
                "result": self._result_for(events[0].aggregate_type,
                                           events[0].payload.get("_id", aggregate_id)) if events else None}

    def _idem_result(self, rec: dict[str, Any]) -> dict[str, Any]:
        return {"status": "idempotent_replay", "first_seq": rec["first_seq"],
                "result": self._result_for(rec["aggregate_type"], rec["aggregate_id"])}

    def _current_version(self, aggregate_type: str, aggregate_id: str) -> int:
        """乐观锁用的当前版本号：采购单取 current_version，其他聚合取已提交事件数。"""

        if aggregate_type == "purchase_order":
            order = self._state["orders"].get(aggregate_id)
            return order.current_version if order is not None else 0
        if aggregate_type == "requisition":
            req = self._state["requisitions"].get(aggregate_id)
            return len(req.history) if req is not None else 0
        return 0

    def _result_for(self, aggregate_type: str, aggregate_id: str) -> dict[str, Any]:
        if aggregate_type == "supplier":
            return self.get_supplier(aggregate_id)
        if aggregate_type == "purchase_order":
            return self.get_order(aggregate_id)
        return self.get_requisition(aggregate_id)

    # ================================================================== #
    # 供应商 API
    # ================================================================== #
    def register_supplier(self, supplier_id: str, name: str, actor: str = "admin",
                          idem_key: str | None = None) -> dict[str, Any]:
        return self._run("supplier", supplier_id,
                         lambda s: self.engine.register_supplier(s, supplier_id, name, actor),
                         idem_key)

    def admit_supplier(self, supplier_id: str, actor: str = "admin",
                       idem_key: str | None = None) -> dict[str, Any]:
        return self._run("supplier", supplier_id,
                         lambda s: self.engine.admit_supplier(s, supplier_id, actor), idem_key)

    def reject_supplier(self, supplier_id: str, reason: str, actor: str = "admin",
                        idem_key: str | None = None) -> dict[str, Any]:
        return self._run("supplier", supplier_id,
                         lambda s: self.engine.reject_supplier(s, supplier_id, reason, actor),
                         idem_key)

    def disable_supplier(self, supplier_id: str, reason: str, actor: str = "admin",
                         idem_key: str | None = None) -> dict[str, Any]:
        return self._run("supplier", supplier_id,
                         lambda s: self.engine.disable_supplier(s, supplier_id, reason, actor),
                         idem_key)

    def enable_supplier(self, supplier_id: str, actor: str = "admin",
                        idem_key: str | None = None) -> dict[str, Any]:
        return self._run("supplier", supplier_id,
                         lambda s: self.engine.enable_supplier(s, supplier_id, actor), idem_key)

    # ================================================================== #
    # 需求 API
    # ================================================================== #
    def create_requisition(self, req_id: str, title: str, requester: str, estimated_amount: float,
                           idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.create_requisition(
                             s, req_id, title, requester, estimated_amount), idem_key)

    def submit_requisition(self, req_id: str, actor: str, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.submit_requisition(s, req_id, actor), idem_key)

    def open_quoting(self, req_id: str, actor: str, deadline: str | None = None,
                     idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.open_quoting(s, req_id, actor, deadline), idem_key)

    def close_quoting(self, req_id: str, actor: str, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.close_quoting(s, req_id, actor), idem_key)

    def cancel_requisition(self, req_id: str, actor: str, reason: str = "",
                           idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.cancel_requisition(s, req_id, actor, reason),
                         idem_key)

    # ---- 报价 ----
    def submit_quote(self, quote_id: str, req_id: str, supplier_id: str, amount: float,
                     valid_until: str, actor: str | None = None,
                     idem_key: str | None = None, accept_late: bool = False) -> dict[str, Any]:
        actor = actor or supplier_id
        return self._run("requisition", req_id,
                         lambda s: self.engine.submit_quote(
                             s, quote_id, req_id, supplier_id, amount, valid_until, actor,
                             accept_late=accept_late),
                         idem_key)

    # ---- 审批 ----
    def start_approval(self, req_id: str, actor: str, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.start_approval(s, req_id, actor), idem_key)

    def approve(self, req_id: str, approver: str, comment: str = "",
                idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.approve(s, req_id, approver, comment), idem_key)

    def reject_approval(self, req_id: str, approver: str, comment: str = "",
                        idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.reject_approval(s, req_id, approver, comment),
                         idem_key)

    def transfer_approval(self, req_id: str, from_user: str, to_user: str, to_role: str,
                          reason: str, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("requisition", req_id,
                         lambda s: self.engine.transfer_approval(
                             s, req_id, from_user, to_user, to_role, reason), idem_key)

    # ---- 采购单 ----
    def create_order(self, order_id: str, req_id: str, amount: float, item: str, quantity: int,
                     expected_delivery: str, note: str = "", actor: str = "buyer",
                     expected_version: int = 0, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("purchase_order", order_id,
                         lambda s: self.engine.create_order(
                             s, order_id, req_id, amount, item, quantity,
                             expected_delivery, note, actor, expected_version),
                         idem_key)

    def change_order(self, order_id: str, changes: dict[str, Any], reason: str, actor: str,
                     expected_version: int, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("purchase_order", order_id,
                         lambda s: self.engine.change_order(
                             s, order_id, changes, reason, actor, expected_version),
                         idem_key, version_baseline=("purchase_order", expected_version))

    def rollback_order(self, order_id: str, target_version: int, reason: str, actor: str,
                       expected_version: int, idem_key: str | None = None) -> dict[str, Any]:
        return self._run("purchase_order", order_id,
                         lambda s: self.engine.rollback_order(
                             s, order_id, target_version, reason, actor, expected_version),
                         idem_key, version_baseline=("purchase_order", expected_version))

    def complete_order(self, order_id: str, actor: str,
                       idem_key: str | None = None) -> dict[str, Any]:
        order = self.get_order(order_id)
        return self._run("requisition", order["requisition_id"],
                         lambda s: self.engine.complete_order(s, order_id, actor), idem_key)

    # ================================================================== #
    # 查询 / 审计
    # ================================================================== #
    def get_requisition(self, req_id: str) -> dict[str, Any]:
        req: Requisition | None = self._state["requisitions"].get(req_id)
        if req is None:
            raise EntityNotFoundError(f"采购需求 {req_id} 不存在", code="REQUISITION_NOT_FOUND")
        return {
            "id": req.id, "title": req.title, "requester": req.requester,
            "estimated_amount": req.estimated_amount, "status": req.status.value,
            "created_at": req.created_at, "quote_deadline": req.quote_deadline,
            "quotes": [self._quote_view(q) for q in sorted(req.quotes.values(), key=lambda q: q.submitted_at)],
            "award": None if req.award is None else {
                "winning_quote_id": req.award.winning_quote_id,
                "winning_supplier_id": req.award.winning_supplier_id,
                "amount": req.award.amount, "reason": req.award.reason,
                "considered_quote_ids": list(req.award.considered_quote_ids),
                "rejected_late_quote_ids": list(req.award.rejected_late_quote_ids),
                "decided_at": req.award.decided_at,
            },
            "approvals": [self._node_view(n) for n in req.approvals],
            "purchase_order_id": req.purchase_order_id,
            "history": [{"from_status": h[0], "to_status": h[1], "action": h[2],
                         "actor": h[3], "at": h[4], "detail": h[5]} for h in req.history],
        }

    def get_order(self, order_id: str) -> dict[str, Any]:
        order: PurchaseOrder | None = self._state["orders"].get(order_id)
        if order is None:
            raise EntityNotFoundError(f"采购单 {order_id} 不存在", code="ORDER_NOT_FOUND")
        snap = order.snapshot()
        snap["requisition_id"] = order.requisition_id
        snap["supplier_id"] = order.supplier_id
        snap["versions"] = [{
            "version": v.version, "amount": v.amount, "item": v.item, "quantity": v.quantity,
            "expected_delivery": v.expected_delivery, "note": v.note,
            "created_at": v.created_at, "change_reason": v.change_reason,
        } for v in order.versions]
        return snap

    def get_supplier(self, supplier_id: str) -> dict[str, Any]:
        sup: Supplier | None = self._state["suppliers"].get(supplier_id)
        if sup is None:
            from .errors import SupplierNotFoundError
            raise SupplierNotFoundError(f"供应商 {supplier_id} 不存在")
        return {
            "id": sup.id, "name": sup.name, "status": sup.status.value,
            "can_participate": sup.status.can_participate,
            "reject_reason": sup.reject_reason,
            "status_history": [{"status": h[0], "reason": h[1], "at": h[2]}
                               for h in sup.status_history],
        }

    def list_suppliers(self) -> list[dict[str, Any]]:
        return [self.get_supplier(sid) for sid in sorted(self._state["suppliers"])]

    def list_requisitions(self) -> list[dict[str, Any]]:
        return [self.get_requisition(rid) for rid in sorted(self._state["requisitions"])]

    def list_orders(self) -> list[dict[str, Any]]:
        return [self.get_order(oid) for oid in sorted(self._state["orders"])]

    def audit_trail(self, aggregate_type: str, aggregate_id: str) -> list[dict[str, Any]]:
        """返回某实体的完整已提交事件链（可追溯依据）。"""

        return [self._event_view(e) for e in
                self.journal.events(aggregate_type, aggregate_id)]

    def list_events(self) -> list[dict[str, Any]]:
        return [self._event_view(e) for e in self.journal.all_events()]

    def recovery_state(self) -> dict[str, Any]:
        """供恢复模块使用：当前投影快照与最后已提交序号。"""

        return {"last_committed_seq": self.journal.last_seq(), "state": self._state}

    def recover(self) -> dict[str, Any]:
        """重启恢复入口：重放 WAL -> 重建内存投影 -> 输出可继续/已提交盘点。"""

        from .recovery import replay

        committed = self.journal.recover()
        self._rebuild()
        return replay(committed).to_dict()

    @staticmethod
    def _event_view(e: Event) -> dict[str, Any]:
        payload = {k: v for k, v in e.payload.items() if not k.startswith("_")}
        return {"seq": e.seq, "aggregate_type": e.aggregate_type,
                "aggregate_id": e.aggregate_id, "kind": e.kind, "payload": payload,
                "actor": e.actor, "at": e.at, "idem_key": e.idem_key}

    @staticmethod
    def _quote_view(q: Quote) -> dict[str, Any]:
        return {"id": q.id, "supplier_id": q.supplier_id, "amount": q.amount,
                "valid_until": q.valid_until, "submitted_at": q.submitted_at,
                "status": q.status.value, "note": q.note}

    @staticmethod
    def _node_view(n: ApprovalNode) -> dict[str, Any]:
        return {"seq": n.seq, "role": n.role, "approver": n.approver,
                "status": n.status.value, "decided_at": n.decided_at,
                "comment": n.comment,
                "transfers": [{"from_user": t[0], "to_user": t[1], "reason": t[2], "at": t[3]}
                              for t in n.transfers]}
