"""FastAPI 端到端：错误码、幂等头、乐观锁头、恢复接口。"""

from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from procurement.api import app, reset_service_for_tests


@pytest.fixture
def client():
    reset_service_for_tests(path=None)  # 内存 Journal
    return TestClient(app)


def _full_flow_to_order(client: TestClient) -> None:
    client.post("/suppliers", json={"supplier_id": "s1", "name": "A"})
    client.post("/suppliers/s1/admit", json={"actor": "admin"})
    client.post("/requisitions", json={
        "req_id": "r1", "title": "t", "requester": "u", "estimated_amount": 5000})
    client.post("/requisitions/r1/submit", json={"actor": "u"},
                headers={"Idempotency-Key": "k-submit"})
    client.post("/requisitions/r1/quoting/open",
                json={"actor": "buyer", "deadline": "2027-02-10T00:00:00+00:00"})
    client.post("/quotes", json={
        "quote_id": "q1", "req_id": "r1", "supplier_id": "s1",
        "amount": 5000, "valid_until": "2027-03-01T00:00:00+00:00"})
    client.post("/requisitions/r1/quoting/close", json={"actor": "buyer"})
    client.post("/requisitions/r1/approvals/start", json={"actor": "buyer"})
    client.post("/requisitions/r1/approvals/approve", json={"actor": "alice"})
    client.post("/orders", json={
        "order_id": "po1", "req_id": "r1", "amount": 5000, "item": "x",
        "quantity": 1, "expected_delivery": "2027-04-01T00:00:00+00:00"})


def test_error_code_mapping(client):
    client.post("/suppliers", json={"supplier_id": "s1", "name": "A"})
    client.post("/suppliers/s1/admit", json={"actor": "admin"})
    client.post("/requisitions", json={
        "req_id": "r1", "title": "t", "requester": "u", "estimated_amount": 5000})
    # 未准入供应商报价 -> 403
    client.post("/suppliers", json={"supplier_id": "sX", "name": "X"})
    client.post("/requisitions/r1/submit", json={"actor": "u"})
    client.post("/requisitions/r1/quoting/open",
                json={"actor": "u", "deadline": "2027-02-10T00:00:00+00:00"})
    resp = client.post("/quotes", json={
        "quote_id": "q", "req_id": "r1", "supplier_id": "sX",
        "amount": 1, "valid_until": "2027-03-01T00:00:00+00:00"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "SUPPLIER_NOT_ADMITTED"
    # 越权审批 -> 403 UNAUTHORIZED_APPROVAL（流程推进到审批）
    client.post("/suppliers/sX/admit", json={"actor": "admin"})
    client.post("/quotes", json={
        "quote_id": "qX", "req_id": "r1", "supplier_id": "sX",
        "amount": 5000, "valid_until": "2027-03-01T00:00:00+00:00"})
    client.post("/requisitions/r1/quoting/close", json={"actor": "u"})
    client.post("/requisitions/r1/approvals/start", json={"actor": "u"})
    resp = client.post("/requisitions/r1/approvals/approve", json={"actor": "eve"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "UNAUTHORIZED_APPROVAL"


def test_idempotency_header(client):
    client.post("/suppliers", json={"supplier_id": "s1", "name": "A"})
    client.post("/suppliers/s1/admit", json={"actor": "admin"})
    client.post("/requisitions", json={
        "req_id": "r1", "title": "t", "requester": "u", "estimated_amount": 5000})
    h = {"Idempotency-Key": "submit"}
    r1 = client.post("/requisitions/r1/submit", json={"actor": "u"}, headers=h)
    r2 = client.post("/requisitions/r1/submit", json={"actor": "u"}, headers=h)
    assert r1.json()["status"] == "ok"
    assert r2.json()["status"] == "idempotent_replay"


def test_order_optimistic_lock_header(client):
    _full_flow_to_order(client)
    h = {"Expected-Version": "1"}
    r = client.post("/orders/po1/changes",
                    json={"changes": {"amount": 4000}, "reason": "降价", "actor": "buyer"},
                    headers=h)
    assert r.status_code == 200
    # 同一过期版本再来 -> 409
    r2 = client.post("/orders/po1/changes",
                     json={"changes": {"amount": 3000}, "reason": "过期基线", "actor": "buyer"},
                     headers=h)
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "VERSION_CONFLICT"


def test_audit_and_recovery_endpoints(client):
    _full_flow_to_order(client)
    trail = client.get("/requisitions/r1/audit").json()
    assert any(e["kind"] == "NODE_APPROVED" for e in trail)
    report = client.post("/admin/recover").json()
    assert report["committed_orders"] == ["po1"]
    assert report["pending_requisitions"] == [
        ["r1", "ORDERED", "complete_order（完结采购单）"]]


def test_get_missing_entity_404(client):
    r = client.get("/requisitions/nope")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REQUISITION_NOT_FOUND"
