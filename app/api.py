"""FastAPI HTTP surface for the procurement workflow.

Every mutating endpoint accepts an `Idempotency-Key` header; retries with
the same key replay the first committed response. WorkflowError reasons are
returned as machine-readable `reason` codes with HTTP 409.
"""
from __future__ import annotations

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import WorkflowError
from .models import POStatus, RequisitionStatus, Role
from .services import (
    ApprovalPolicy,
    ApprovalService,
    OrderService,
    QuoteService,
    RequisitionService,
    SupplierService,
)
from .store import Store


class SupplierIn(BaseModel):
    name: str
    id: str | None = None


class RequisitionIn(BaseModel):
    title: str
    amount: float
    id: str | None = None


class QuoteIn(BaseModel):
    supplier_id: str
    amount: float
    valid_until: str  # ISO date, e.g. "2026-12-31"
    id: str | None = None


class CloseQuotingIn(BaseModel):
    now: str  # ISO date used as adjudication time


class ApprovalStartIn(BaseModel):
    assignees: dict[str, str] = {}


class DecisionIn(BaseModel):
    actor_id: str
    actor_role: Role


class TransferIn(BaseModel):
    new_assignee: str


class TransitionIn(BaseModel):
    target: str


class AmendIn(BaseModel):
    base_version: int
    changes: dict


class RollbackIn(BaseModel):
    target_version: int


def create_app(db_path: str = ":memory:",
               approval_policy: ApprovalPolicy | None = None) -> FastAPI:
    store = Store(db_path)
    suppliers = SupplierService(store)
    requisitions = RequisitionService(store)
    quotes = QuoteService(store, suppliers, requisitions)
    approvals = ApprovalService(store, requisitions, approval_policy)
    orders = OrderService(store, suppliers, requisitions)

    app = FastAPI(title="Supplier Procurement Workflow")
    app.state.store = store

    @app.exception_handler(WorkflowError)
    async def workflow_error(_: Request, exc: WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=409,
                            content={"reason": exc.reason.value, "detail": str(exc)})

    def key(k: str | None) -> str | None:
        return k

    # -- suppliers ------------------------------------------------------
    @app.post("/suppliers", status_code=201)
    def register_supplier(body: SupplierIn,
                          idempotency_key: str | None = Header(default=None)):
        return suppliers.register(body.name, body.id, key(idempotency_key))

    @app.get("/suppliers/{sid}")
    def get_supplier(sid: str):
        return suppliers.get(sid)

    @app.post("/suppliers/{sid}/admit")
    def admit_supplier(sid: str, idempotency_key: str | None = Header(default=None)):
        return suppliers.admit(sid, key(idempotency_key))

    @app.post("/suppliers/{sid}/suspend")
    def suspend_supplier(sid: str, idempotency_key: str | None = Header(default=None)):
        return suppliers.suspend(sid, key(idempotency_key))

    @app.post("/suppliers/{sid}/reinstate")
    def reinstate_supplier(sid: str, idempotency_key: str | None = Header(default=None)):
        return suppliers.reinstate(sid, key(idempotency_key))

    # -- requisitions ----------------------------------------------------
    @app.post("/requisitions", status_code=201)
    def create_requisition(body: RequisitionIn,
                           idempotency_key: str | None = Header(default=None)):
        return requisitions.create(body.title, body.amount, body.id,
                                   key(idempotency_key))

    @app.get("/requisitions/{rid}")
    def get_requisition(rid: str):
        return requisitions.get(rid)

    @app.post("/requisitions/{rid}/transition")
    def transition_requisition(rid: str, body: TransitionIn,
                               idempotency_key: str | None = Header(default=None)):
        return requisitions.advance(rid, RequisitionStatus(body.target),
                                    key(idempotency_key))

    # -- quotes -----------------------------------------------------------
    @app.post("/requisitions/{rid}/quotes", status_code=201)
    def submit_quote(rid: str, body: QuoteIn,
                     idempotency_key: str | None = Header(default=None)):
        return quotes.submit(rid, body.supplier_id, body.amount,
                             body.valid_until, body.id, key(idempotency_key))

    @app.get("/requisitions/{rid}/quotes")
    def list_quotes(rid: str):
        return quotes.list_for(rid)

    @app.post("/requisitions/{rid}/close-quoting")
    def close_quoting(rid: str, body: CloseQuotingIn,
                      idempotency_key: str | None = Header(default=None)):
        return quotes.close(rid, body.now, key(idempotency_key))

    # -- approvals ---------------------------------------------------------
    @app.post("/requisitions/{rid}/approvals", status_code=201)
    def start_approvals(rid: str, body: ApprovalStartIn,
                        idempotency_key: str | None = Header(default=None)):
        return approvals.start(rid, body.assignees, key(idempotency_key))

    @app.get("/requisitions/{rid}/approvals")
    def list_approvals(rid: str):
        return approvals.list_for(rid)

    @app.post("/approvals/{aid}/approve")
    def approve(aid: str, body: DecisionIn,
                idempotency_key: str | None = Header(default=None)):
        return approvals.approve(aid, body.actor_id, body.actor_role,
                                 key(idempotency_key))

    @app.post("/approvals/{aid}/reject")
    def reject(aid: str, body: DecisionIn,
               idempotency_key: str | None = Header(default=None)):
        return approvals.reject(aid, body.actor_id, body.actor_role,
                                key(idempotency_key))

    @app.post("/approvals/{aid}/transfer")
    def transfer(aid: str, body: TransferIn,
                 idempotency_key: str | None = Header(default=None)):
        return approvals.transfer(aid, body.new_assignee, key(idempotency_key))

    # -- purchase orders ---------------------------------------------------
    @app.post("/requisitions/{rid}/purchase-orders", status_code=201)
    def create_po(rid: str, idempotency_key: str | None = Header(default=None)):
        return orders.create_from_requisition(rid, key=idempotency_key)

    @app.get("/purchase-orders/{pid}")
    def get_po(pid: str):
        return orders.get(pid)

    @app.get("/purchase-orders/{pid}/versions")
    def po_versions(pid: str):
        return orders.versions(pid)

    @app.post("/purchase-orders/{pid}/transition")
    def transition_po(pid: str, body: TransitionIn,
                      idempotency_key: str | None = Header(default=None)):
        return orders.advance(pid, POStatus(body.target), key(idempotency_key))

    @app.post("/purchase-orders/{pid}/amend")
    def amend_po(pid: str, body: AmendIn,
                 idempotency_key: str | None = Header(default=None)):
        return orders.amend(pid, body.base_version, body.changes,
                            key(idempotency_key))

    @app.post("/purchase-orders/{pid}/rollback")
    def rollback_po(pid: str, body: RollbackIn,
                    idempotency_key: str | None = Header(default=None)):
        return orders.rollback(pid, body.target_version, key(idempotency_key))

    # -- audit --------------------------------------------------------------
    @app.get("/events/{entity}/{entity_id}")
    def events(entity: str, entity_id: str):
        return store.events_for(entity, entity_id)

    return app
