"""需求 5：失败不留部分写入、重启区分已提交/未完成并从最近提交点继续。"""

from __future__ import annotations

import json
import os

import pytest

from procurement.errors import PreconditionFailedError
from procurement.journal import Journal
from procurement.recovery import is_resumable_status
from procurement.service import ProcurementService

from .conftest import FakeClock


def make(path, clock_value="2026-01-01T00:00:00+00:00"):
    return ProcurementService(Journal(path), clock=FakeClock(clock_value))


def test_failed_command_leaves_no_partial_state_or_events(svc):
    svc.register_supplier("s1", "A")
    svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    seq_before = svc.journal.last_seq()
    # 裁决失败：唯一报价已过期，没有有效报价 -> 前置条件拒绝
    # 直接构造一个过期场景的新需求更清晰：
    svc.create_requisition("r2", "t2", "u", 5000)
    svc.submit_requisition("r2", "u")
    svc.open_quoting("r2", "u", deadline="2026-02-10T00:00:00+00:00")
    # 无报价直接裁决 -> 失败
    with pytest.raises(PreconditionFailedError) as ei:
        svc.close_quoting("r2", "u")
    assert ei.value.code == "NO_ELIGIBLE_QUOTE"
    # 没有任何新事件落盘，需求仍停在 QUOTING（可继续）
    assert svc.journal.last_seq() == seq_before + 3  # 仅 r2 的 create/submit/open
    assert svc.get_requisition("r2")["status"] == "QUOTING"
    assert is_resumable_status("QUOTING")


def test_wal_replays_only_committed_transactions(wal_path):
    """手工构造 WAL：一个已提交事务 + 一个只有 prepare 没有 commit 的中断事务。"""

    svc = make(wal_path)
    svc.register_supplier("s1", "A")
    svc.admit_supplier("s1")
    # 追加一条孤立的 prepare 行（模拟崩溃在 commit 前）
    with open(wal_path, "a", encoding="utf-8") as f:
        orphan = {"t": "event", "event": {
            "seq": svc.journal.last_seq() + 1, "aggregate_type": "requisition",
            "aggregate_id": "ghost", "kind": "REQUISITION_CREATED",
            "payload": {"_id": "ghost"}, "actor": "u", "at": "t", "idem_key": None}}
        f.write(json.dumps(orphan, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    # 重启：未提交的 ghost 不得出现
    svc2 = make(wal_path)
    report = svc2.recover()
    assert "ghost" not in [r["id"] for r in svc2.list_requisitions()]
    assert svc2.get_supplier("s1")["status"] == "ADMITTED"
    # 报告事件数等于已提交数量（不含孤儿）
    assert report["event_count"] == svc.journal.last_seq()


def test_restart_resumes_from_last_committed_point(wal_path):
    svc = make(wal_path)
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    # 中断在报价阶段 -> 重启
    svc2 = make(wal_path)
    report = svc2.recover()
    assert report["pending_requisitions"] == [
        ["r1", "QUOTING", "供应商报价；完成后 close_quoting 裁决"]]
    assert report["committed_orders"] == []
    # 从最近提交点继续直到完结
    svc2.close_quoting("r1", "u")
    svc2.start_approval("r1", "u")
    svc2.approve("r1", "alice")
    svc2.create_order("po1", "r1", 5000, "item", 1, "2026-04-01T00:00:00+00:00")
    # 再次重启
    svc3 = make(wal_path)
    report3 = svc3.recover()
    assert report3["committed_orders"] == ["po1"]
    pending = dict((x[0], x) for x in report3["pending_requisitions"])
    assert pending["r1"][1] == "ORDERED"
    assert "complete_order" in pending["r1"][2]
    assert report3["approved_not_ordered"] == []
    assert svc3.get_order("po1")["amount"] == 5000
    svc3.complete_order("po1", "u")
    assert svc3.get_requisition("r1")["status"] == "CLOSED"


def test_restart_with_approved_not_ordered_flag(wal_path):
    svc = make(wal_path)
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u")
    svc.open_quoting("r1", "u", deadline="2026-02-10T00:00:00+00:00")
    svc.submit_quote("q1", "r1", "s1", 5000, "2026-03-01T00:00:00+00:00")
    svc.close_quoting("r1", "u")
    svc.start_approval("r1", "u")
    svc.approve("r1", "alice")
    # 已审批但下单前崩溃
    svc2 = make(wal_path)
    report = svc2.recover()
    assert report["approved_not_ordered"] == ["r1"]
    pending = dict((x[0], x) for x in report["pending_requisitions"])
    assert "create_order" in pending["r1"][2]


def test_idempotency_survives_restart(wal_path):
    svc = make(wal_path)
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 5000)
    svc.submit_requisition("r1", "u", idem_key="k1")
    svc2 = make(wal_path)
    # 重启后同一幂等键 -> 回放，不产生事件
    before = svc2.journal.last_seq()
    out = svc2.submit_requisition("r1", "u", idem_key="k1")
    assert out["status"] == "idempotent_replay"
    assert svc2.journal.last_seq() == before


def test_event_seq_contiguous_and_audit_intact_after_recovery(wal_path):
    svc = make(wal_path)
    svc.register_supplier("s1", "A"); svc.admit_supplier("s1")
    svc.create_requisition("r1", "t", "u", 5000)
    svc2 = make(wal_path)
    seqs = [e.seq for e in svc2.journal.all_events()]
    assert seqs == list(range(1, len(seqs) + 1))
