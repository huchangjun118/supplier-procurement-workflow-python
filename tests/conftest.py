"""测试夹具：可控时钟 + 每测试独立服务实例（内存与 WAL 两种后端）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from procurement.journal import Journal
from procurement.service import ProcurementService


class FakeClock:
    """测试用时钟：固定基准时间，可手动推进。"""

    def __init__(self, start: str = "2026-01-01T00:00:00+00:00") -> None:
        self.now_value = start

    def __call__(self) -> str:
        return self.now_value

    def set(self, value: str) -> None:
        self.now_value = value


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def svc(clock: FakeClock) -> ProcurementService:
    return ProcurementService(Journal(), clock=clock)


@pytest.fixture
def wal_path() -> str:
    fd, path = tempfile.mkstemp(suffix=".wal")
    os.close(fd)
    os.remove(path)  # Journal 期望文件可能不存在
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def make_svc(wal_path):
    """构造服务的工厂；可反复用同一 WAL 路径构造，模拟重启。"""

    created: list[ProcurementService] = []

    def _make(clock_value: str = "2026-01-01T00:00:00+00:00", path: str | None = wal_path):
        clk = FakeClock(clock_value)
        service = ProcurementService(Journal(path), clock=clk)
        created.append(service)
        return service, clk

    return _make


def seed_ready_to_quote(svc: ProcurementService, suppliers=("s1", "s2"),
                        req_id: str = "r1", amount: float = 150000,
                        clock: FakeClock | None = None) -> None:
    """把流程推进到 QUOTING：注册并准入供应商 -> 建需求 -> 提交 -> 开放报价。"""

    for i, sid in enumerate(suppliers):
        svc.register_supplier(sid, f"供应商{sid}")
        svc.admit_supplier(sid)
    svc.create_requisition(req_id, "采购测试", "buyer", amount)
    svc.submit_requisition(req_id, "buyer")
    svc.open_quoting(req_id, "buyer", deadline="2026-02-10T00:00:00+00:00")
