"""测试辅助：构造任务、临时 spool 与可控时钟。"""
from __future__ import annotations

import json
import pathlib
from form_rpa.models import FormRecord
from form_rpa.queue import create_task
from form_rpa.receipts import ReceiptStore


from form_rpa.clock import FakeClock  # noqa: F401


def make_store(path: pathlib.Path, lease_seconds: int = 30,
               clock: FakeClock | None = None) -> ReceiptStore:
    return ReceiptStore(path, lease_seconds=lease_seconds,
                        clock=clock if clock is not None else None)


def make_task(record_id: str = "R-1", amount: int = 100):
    record = FormRecord(
        record_id=record_id, customer_code="C-1", amount=amount,
        submitted_at="2026-11-10", fields={"name": "Alice"})
    return create_task(record)


def seed_task(store: ReceiptStore, record_id: str = "R-1", amount: int = 100):
    task = make_task(record_id, amount)
    assert store.enqueue(task)
    return task


def write_fixture(path: pathlib.Path, rows: list[dict]) -> pathlib.Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path
