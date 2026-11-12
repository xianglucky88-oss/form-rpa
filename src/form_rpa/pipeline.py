from __future__ import annotations
from .inputs import FormSource
from .queue import create_task
from .receipts import ReceiptStore
from .validate import validate_record


class FormPipeline:
    def __init__(self, source: FormSource, store: ReceiptStore) -> None:
        self.source = source
        self.store = store

    def run(self) -> tuple[int, int]:
        accepted = 0
        duplicates = 0
        for raw in self.source.read():
            try:
                record = validate_record(raw)
                if self.store.enqueue(create_task(record)):
                    accepted += 1
                else:
                    duplicates += 1
            except (TypeError, ValueError, KeyError) as exc:
                record_id = str(raw.get("record_id", "")) if isinstance(raw, dict) else ""
                self.store.record_rejection(record_id, raw, str(exc))
        return accepted, duplicates
