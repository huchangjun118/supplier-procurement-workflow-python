"""FastAPI 适配层：把工作流能力暴露为 HTTP 接口。

- 所有错误统一映射为 {error: {code, message}}，HTTP 409/422/404 语义化；
- 幂等键支持请求头 Idempotency-Key；
- 乐观锁版本支持请求头 Expected-Version；
- 服务实例默认使用本地 WAL 文件，重启自动恢复。
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    ConflictError,
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
    WorkflowError,
)
from .journal import Journal
from .service import ProcurementService

WAL_PATH = os.environ.get("PROCUREMENT_WAL", os.path.join(os.getcwd(), "procurement.wal"))

_service: ProcurementService | None = None


def get_service() -> ProcurementService:
    global _service
    if _service is None:
        _service = ProcurementService(Journal(WAL_PATH))
    return _service


def reset_service_for_tests(path: str | None = None) -> ProcurementService:
    global _service
    _service = ProcurementService(Journal(path))
    return _service


app = FastAPI(title="采购协同工作流", version="1.0.0")

_HTTP_STATUS = {
    IllegalTransitionError: 409,
    DuplicateSubmitError: 409,
    ConflictError: 409,
    UnauthorizedApprovalError: 403,
    TransferError: 403,
    SupplierNotAdmittedError: 403,
    SupplierDisabledError: 403,
    PreconditionFailedError: 422,
    QuoteRejectedError: 422,
    EntityNotFoundError: 404,
    SupplierNotFoundError: 404,
}


@app.exception_handler(WorkflowError)
async def workflow_error_handler(request: Request, exc: WorkflowError) -> JSONResponse:
    status = _HTTP_STATUS.get(type(exc), 400)
    return JSONResponse(status_code=status,
                        content={"error": {"code": exc.code, "message": exc.message}})


# --------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------- #
class RegisterSupplierIn(BaseModel):
    supplier_id: str
    name: str
    actor: str = "admin"


class RejectSupplierIn(BaseModel):
    reason: str
    actor: str = "admin"


class SimpleActorIn(BaseModel):
    actor: str = "admin"


class CreateRequisitionIn(BaseModel):
    req_id: str
    title: str
    requester: str
    estimated_amount: float = Field(ge=0)


class SubmitRequisitionIn(BaseModel):
    actor: str


class OpenQuotingIn(BaseModel):
    actor: str
    deadline: str | None = None


class ActorIn(BaseModel):
    actor: str
    reason: str = ""


class QuoteIn(BaseModel):
    quote_id: str
    req_id: str
    supplier_id: str
    amount: float = Field(gt=0)
    valid_until: str
    actor: str | None = None


class DecisionIn(BaseModel):
    actor: str
    comment: str = ""


class TransferIn(BaseModel):
    from_user: str
    to_user: str
    to_role: str
    reason: str


class CreateOrderIn(BaseModel):
    order_id: str
    req_id: str
    amount: float = Field(gt=0)
    item: str
    quantity: int = Field(gt=0)
    expected_delivery: str
    note: str = ""
    actor: str = "buyer"


class ChangeOrderIn(BaseModel):
    changes: dict[str, Any]
    reason: str
    actor: str


class RollbackOrderIn(BaseModel):
    target_version: int
    reason: str
    actor: str


# --------------------------------------------------------------------- #
# 供应商路由
# --------------------------------------------------------------------- #
@app.post("/suppliers")
def register_supplier(body: RegisterSupplierIn, idempotency_key: str | None = Header(default=None)):
    return get_service().register_supplier(
        body.supplier_id, body.name, body.actor, idem_key=idempotency_key)


@app.post("/suppliers/{supplier_id}/admit")
def admit(supplier_id: str, body: SimpleActorIn, idempotency_key: str | None = Header(default=None)):
    return get_service().admit_supplier(supplier_id, body.actor, idem_key=idempotency_key)


@app.post("/suppliers/{supplier_id}/reject")
def reject(supplier_id: str, body: RejectSupplierIn, idempotency_key: str | None = Header(default=None)):
    return get_service().reject_supplier(supplier_id, body.reason, body.actor, idem_key=idempotency_key)


@app.post("/suppliers/{supplier_id}/disable")
def disable(supplier_id: str, body: RejectSupplierIn, idempotency_key: str | None = Header(default=None)):
    return get_service().disable_supplier(supplier_id, body.reason, body.actor, idem_key=idempotency_key)


@app.post("/suppliers/{supplier_id}/enable")
def enable(supplier_id: str, body: SimpleActorIn, idempotency_key: str | None = Header(default=None)):
    return get_service().enable_supplier(supplier_id, body.actor, idem_key=idempotency_key)


@app.get("/suppliers")
def list_suppliers():
    return get_service().list_suppliers()


@app.get("/suppliers/{supplier_id}")
def get_supplier(supplier_id: str):
    return get_service().get_supplier(supplier_id)


# --------------------------------------------------------------------- #
# 需求 / 报价 / 审批路由
# --------------------------------------------------------------------- #
@app.post("/requisitions")
def create_requisition(body: CreateRequisitionIn, idempotency_key: str | None = Header(default=None)):
    return get_service().create_requisition(
        body.req_id, body.title, body.requester, body.estimated_amount, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/submit")
def submit_requisition(req_id: str, body: SubmitRequisitionIn,
                       idempotency_key: str | None = Header(default=None)):
    return get_service().submit_requisition(req_id, body.actor, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/quoting/open")
def open_quoting(req_id: str, body: OpenQuotingIn,
                 idempotency_key: str | None = Header(default=None)):
    return get_service().open_quoting(req_id, body.actor, body.deadline, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/quoting/close")
def close_quoting(req_id: str, body: SimpleActorIn,
                  idempotency_key: str | None = Header(default=None)):
    return get_service().close_quoting(req_id, body.actor, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/cancel")
def cancel(req_id: str, body: ActorIn, idempotency_key: str | None = Header(default=None)):
    return get_service().cancel_requisition(req_id, body.actor, body.reason, idem_key=idempotency_key)


@app.post("/quotes")
def submit_quote(body: QuoteIn, idempotency_key: str | None = Header(default=None)):
    return get_service().submit_quote(
        body.quote_id, body.req_id, body.supplier_id, body.amount,
        body.valid_until, body.actor, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/approvals/start")
def start_approval(req_id: str, body: SimpleActorIn,
                   idempotency_key: str | None = Header(default=None)):
    return get_service().start_approval(req_id, body.actor, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/approvals/approve")
def approve(req_id: str, body: DecisionIn, idempotency_key: str | None = Header(default=None)):
    return get_service().approve(req_id, body.actor, body.comment, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/approvals/reject")
def reject_approval(req_id: str, body: DecisionIn,
                    idempotency_key: str | None = Header(default=None)):
    return get_service().reject_approval(req_id, body.actor, body.comment, idem_key=idempotency_key)


@app.post("/requisitions/{req_id}/approvals/transfer")
def transfer(req_id: str, body: TransferIn, idempotency_key: str | None = Header(default=None)):
    return get_service().transfer_approval(
        req_id, body.from_user, body.to_user, body.to_role, body.reason, idem_key=idempotency_key)


@app.get("/requisitions")
def list_requisitions():
    return get_service().list_requisitions()


@app.get("/requisitions/{req_id}")
def get_requisition(req_id: str):
    return get_service().get_requisition(req_id)


@app.get("/requisitions/{req_id}/audit")
def audit_requisition(req_id: str):
    return get_service().audit_trail("requisition", req_id)


# --------------------------------------------------------------------- #
# 采购单路由
# --------------------------------------------------------------------- #
@app.post("/orders")
def create_order(body: CreateOrderIn, idempotency_key: str | None = Header(default=None)):
    return get_service().create_order(
        body.order_id, body.req_id, body.amount, body.item, body.quantity,
        body.expected_delivery, body.note, body.actor, idem_key=idempotency_key)


@app.post("/orders/{order_id}/changes")
def change_order(order_id: str, body: ChangeOrderIn,
                 expected_version: int | None = Header(default=None),
                 idempotency_key: str | None = Header(default=None)):
    return get_service().change_order(
        order_id, body.changes, body.reason, body.actor,
        expected_version if expected_version is not None else 0, idem_key=idempotency_key)


@app.post("/orders/{order_id}/rollback")
def rollback_order(order_id: str, body: RollbackOrderIn,
                   expected_version: int | None = Header(default=None),
                   idempotency_key: str | None = Header(default=None)):
    return get_service().rollback_order(
        order_id, body.target_version, body.reason, body.actor,
        expected_version if expected_version is not None else 0, idem_key=idempotency_key)


@app.post("/orders/{order_id}/complete")
def complete_order(order_id: str, body: SimpleActorIn,
                   idempotency_key: str | None = Header(default=None)):
    return get_service().complete_order(order_id, body.actor, idem_key=idempotency_key)


@app.get("/orders")
def list_orders():
    return get_service().list_orders()


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    return get_service().get_order(order_id)


@app.get("/orders/{order_id}/audit")
def audit_order(order_id: str):
    return get_service().audit_trail("purchase_order", order_id)


# --------------------------------------------------------------------- #
# 恢复 / 全局审计
# --------------------------------------------------------------------- #
@app.post("/admin/recover")
def recover():
    return get_service().recover()


@app.get("/admin/events")
def all_events():
    return get_service().list_events()


@app.get("/")
def index():
    return {
        "service": "supplier-procurement-workflow",
        "wal": WAL_PATH,
        "last_committed_seq": get_service().journal.last_seq(),
        "docs": "/docs",
    }
