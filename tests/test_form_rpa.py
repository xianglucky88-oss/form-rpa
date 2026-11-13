"""基线行为回归：幂等入队与坏数据隔离。"""
from __future__ import annotations

import pathlib

from form_rpa.inputs import JsonFormSource
from form_rpa.pipeline import FormPipeline
from form_rpa.receipts import ReceiptStore


def test_form_queue_is_idempotent(tmp_path):
    path = tmp_path / "forms.json"
    path.write_text(
        '[{"record_id":"R-1","customer_code":"c-1","amount":10,"name":"A"},'
        '{"record_id":"R-1","customer_code":"C-1","amount":10,"name":"A"}]')
    store = ReceiptStore(tmp_path / "forms.sqlite3")
    try:
        accepted, duplicates = FormPipeline(JsonFormSource(path), store).run()
        assert (accepted, duplicates) == (1, 1)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM submission_tasks").fetchone()[0] == 1
    finally:
        store.close()


def test_invalid_row_is_quarantined_without_stopping_batch(tmp_path):
    path = tmp_path / "forms.json"
    path.write_text(
        '[{"record_id":"BAD","customer_code":"!","amount":10},'
        '{"record_id":"OK-1","customer_code":"C-1","amount":10}]')
    store = ReceiptStore(tmp_path / "forms.sqlite3")
    try:
        accepted, duplicates = FormPipeline(JsonFormSource(path), store).run()
        assert (accepted, duplicates) == (1, 0)
        assert store.connection.execute(
            "SELECT COUNT(*) FROM submission_rejections").fetchone()[0] == 1
        payload = store.connection.execute(
            "SELECT payload FROM submission_tasks").fetchone()[0]
        assert '"customer_code": "C-1"' in payload
    finally:
        store.close()


def test_payload_idempotency_meaning_is_preserved(tmp_path):
    """同 record_id 但 payload 变化应产生不同幂等键（沿用基线语义）。"""
    from form_rpa.queue import create_task
    from form_rpa.validate import validate_record
    t1 = create_task(validate_record(
        {"record_id": "R-9", "customer_code": "C-1", "amount": 10}))
    t2 = create_task(validate_record(
        {"record_id": "R-9", "customer_code": "C-1", "amount": 20}))
    assert t1.idempotency_key != t2.idempotency_key
    assert pathlib.Path(tmp_path).exists()  # 仅为使用 tmp_path 夹具
