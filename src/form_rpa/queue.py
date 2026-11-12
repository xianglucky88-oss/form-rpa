from __future__ import annotations
import hashlib
import json
from .models import FormRecord, SubmissionTask


def create_task(record: FormRecord) -> SubmissionTask:
    payload = {"customer_code": record.customer_code, "amount": str(record.amount), **record.fields}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    key = hashlib.sha256(f"{record.record_id}|{canonical}".encode("utf-8")).hexdigest()
    return SubmissionTask(key, record.record_id, payload, "pending")
