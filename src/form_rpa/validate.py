from __future__ import annotations
import re
from .models import FormRecord


def validate_record(raw: dict) -> FormRecord:
    record_id = str(raw.get("record_id", "")).strip()
    customer_code = str(raw.get("customer_code", "")).strip().upper()
    if not record_id:
        raise ValueError("record_id is required")
    if not re.fullmatch(r"[A-Z0-9-]{2,32}", customer_code):
        raise ValueError("customer_code must be alphanumeric")
    amount = int(raw.get("amount", 0))
    if amount <= 0:
        raise ValueError("amount must be positive")
    fields = {str(key): str(value).strip() for key, value in raw.items() if key not in {"record_id", "customer_code", "amount"}}
    return FormRecord(record_id, customer_code, amount, str(raw.get("submitted_at", "")), fields)
