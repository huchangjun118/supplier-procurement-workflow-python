"""事件日志与事务性存储（两阶段 WAL）。

提交协议：
1. prepare：事务内每个事件追加一行 {"t":"event", ...} 并 fsync；
2. commit：事务成功后追加一行 {"t":"commit"} 并 fsync；
3. 重放时：event 行先进入暂存区，只有读到紧随的 commit 行才算已提交；
   崩溃/中止留下的尾部未 commit event 行一律丢弃 -> 不会有部分写入。

单把可重入锁串行化所有写事务：并发提交（报价/审批）经过同一临界区，
裁决结果因此确定、可复现。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Event:
    """一条已提交的状态变更事件（审计链的最小单元）。"""

    seq: int
    aggregate_type: str       # 'requisition' | 'purchase_order' | 'supplier'
    aggregate_id: str
    kind: str                 # 动作类型，如 'REQUISITION_SUBMITTED' / 'QUOTE_SUBMITTED'
    payload: dict[str, Any]
    actor: str
    at: str
    idem_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "kind": self.kind,
            "payload": self.payload,
            "actor": self.actor,
            "at": self.at,
            "idem_key": self.idem_key,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        return cls(
            seq=d["seq"],
            aggregate_type=d["aggregate_type"],
            aggregate_id=d["aggregate_id"],
            kind=d["kind"],
            payload=d["payload"],
            actor=d["actor"],
            at=d["at"],
            idem_key=d.get("idem_key"),
        )


class Journal:
    """线程安全的事件存储（内存 + 可选 WAL 文件）。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._events: list[Event] = []
        if path is not None:
            self._events = self._read_wal(path)

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def transactionally(self, fn: Callable[["EventAppender"], Any]) -> Any:
        """在单个串行临界区内执行 fn(appender)。

        fn 通过 appender.emit(...) 产生事件（prepare 阶段先落 WAL）；
        fn 正常返回后写 commit 标记并把事件并入只读日志；
        fn 抛异常则不写 commit 标记，本事务全部事件对任何人都不可见。
        """

        with self._lock:
            staged: list[Event] = []
            appender = EventAppender(self, staged)
            try:
                result = fn(appender)
            except BaseException:
                staged.clear()
                raise
            if staged:
                self._write_commit()
            self._events.extend(staged)
            return result

    def append(self, event: Event) -> None:
        """直接提交单条事件（prepare + commit 一次完成）。"""

        with self._lock:
            self._write_prepare(event)
            self._write_commit()
            self._events.append(event)

    def events(self, aggregate_type: str | None = None, aggregate_id: str | None = None) -> list[Event]:
        with self._lock:
            out = list(self._events)
        if aggregate_type is not None:
            out = [e for e in out if e.aggregate_type == aggregate_type]
        if aggregate_id is not None:
            out = [e for e in out if e.aggregate_id == aggregate_id]
        return out

    def all_events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def last_seq(self) -> int:
        with self._lock:
            return self._events[-1].seq if self._events else 0

    def recover(self) -> list[Event]:
        """从 WAL 文件重放全部已提交事件（无文件路径时返回当前内存事件）。"""

        if self.path is None:
            return self.all_events()
        with self._lock:
            recovered = self._read_wal(self.path)
            self._events = recovered
            return list(recovered)

    # ------------------------------------------------------------------ #
    # 内部实现（调用方须持有 self._lock）
    # ------------------------------------------------------------------ #
    def last_seq_locked(self) -> int:
        return self._events[-1].seq if self._events else 0

    def _write_line(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _write_prepare(self, event: Event) -> None:
        self._write_line({"t": "event", "event": event.to_dict()})

    def _write_commit(self) -> None:
        self._write_line({"t": "commit"})

    @staticmethod
    def _read_wal(path: str) -> list[Event]:
        if not os.path.exists(path):
            return []
        committed: list[Event] = []
        staged: list[Event] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # 崩溃导致的半截写：忽略该行及之后所有内容
                    break
                kind = record.get("t")
                if kind == "event":
                    try:
                        staged.append(Event.from_dict(record["event"]))
                    except (KeyError, TypeError):
                        break
                elif kind == "commit":
                    committed.extend(staged)
                    staged.clear()
                else:
                    break
        # 文件结束时仍滞留在 staged 的事件属于未提交事务，丢弃。
        return committed


class EventAppender:
    """事务内部的事件发射器。"""

    def __init__(self, journal: Journal, staged: list[Event]) -> None:
        self._journal = journal
        self._staged = staged

    def emit(self, aggregate_type: str, aggregate_id: str, kind: str, payload: dict[str, Any],
             actor: str, at: str, idem_key: str | None = None) -> Event:
        seq = self._journal.last_seq_locked() + len(self._staged) + 1
        event = Event(seq, aggregate_type, aggregate_id, kind, payload, actor, at, idem_key)
        # prepare：先持久化；落盘失败则事件不会进入提交列表
        self._journal._write_prepare(event)
        self._staged.append(event)
        return event
