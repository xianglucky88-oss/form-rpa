from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class FormRecord:
    record_id: str
    customer_code: str
    amount: int
    submitted_at: str
    fields: dict[str, str]


@dataclass(frozen=True)
class SubmissionTask:
    idempotency_key: str
    record_id: str
    payload: dict[str, str]
    state: str
