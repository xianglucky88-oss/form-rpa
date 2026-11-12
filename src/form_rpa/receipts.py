from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .models import SubmissionTask


_ALLOWED_STATES = {"pending", "running", "succeeded", "business_failed", "login_failed", "page_changed", "rejected"}


class ReceiptStore:
    """负责任务、回执状态和输入拒绝记录的持久化；不执行浏览器动作。"""

    def __init__(self, database: str | Path) -> None:
        self.connection = sqlite3.connect(database)
        self.connection.execute("""CREATE TABLE IF NOT EXISTS submission_tasks (
            idempotency_key TEXT PRIMARY KEY, record_id TEXT NOT NULL,
            payload TEXT NOT NULL, state TEXT NOT NULL, message TEXT
        )""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS submission_rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT, error TEXT NOT NULL,
            raw_payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        self.connection.commit()

    def enqueue(self, task: SubmissionTask) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO submission_tasks VALUES (?, ?, ?, ?, ?)",
            (task.idempotency_key, task.record_id,
             json.dumps(task.payload, ensure_ascii=False, sort_keys=True), task.state, None),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def record_result(self, key: str, state: str, message: str = "") -> None:
        if state not in _ALLOWED_STATES:
            raise ValueError(f"unsupported task state: {state}")
        cursor = self.connection.execute(
            "UPDATE submission_tasks SET state = ?, message = ? WHERE idempotency_key = ?",
            (state, message, key),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown task: {key}")
        self.connection.commit()

    def record_rejection(self, record_id: str, raw: object, error: str) -> None:
        self.connection.execute(
            "INSERT INTO submission_rejections(record_id, error, raw_payload) VALUES (?, ?, ?)",
            (record_id, error, json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
