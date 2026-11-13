"""Python 与 Node 浏览器执行器之间的文件交接协议。

目录布局（spool 根目录）::

    requests/<attempt_id>.json     Python 写，Node 只读（原子 rename）
    progress/<attempt_id>.jsonl    Node 追加阶段检查点（每行 fsync）
    results/<attempt_id>.json      Node 原子写最终结果
    evidence/<attempt_id>/...      截图、HTML 等证据文件

安全约定
--------
- 请求文件不含口令；密钥只通过环境变量传给执行器子进程。
- 所有写入走“临时文件 + fsync + rename”，读取方不会看到半截文件。
- 解析外部结果全部做结构校验，拒绝缺字段/未知枚举的文件。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .models import (
    ALLOWED_OUTCOMES, PHASE_ASSIGNED, PHASE_DONE, Attempt, AttemptResult,
)

REQUESTS = "requests"
PROGRESS = "progress"
RESULTS = "results"
EVIDENCE = "evidence"


class HandoffSpool:
    def __init__(self, root: str | Path) -> None:
        # 统一解析为绝对路径：子进程 cwd 与父进程不同，
        # 交接文件中的所有路径必须与进程工作目录无关
        self.root = Path(root).resolve()
        for name in (REQUESTS, PROGRESS, RESULTS, EVIDENCE):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- 写请求

    def request_path(self, attempt_id: str) -> Path:
        return self.root / REQUESTS / f"{attempt_id}.json"

    def progress_path(self, attempt_id: str) -> Path:
        return self.root / PROGRESS / f"{attempt_id}.jsonl"

    def result_path(self, attempt_id: str) -> Path:
        return self.root / RESULTS / f"{attempt_id}.json"

    def evidence_dir(self, attempt_id: str) -> Path:
        path = self.root / EVIDENCE / attempt_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_request(self, attempt: Attempt, portal_url: str, form_id: str,
                      payload: dict, dry_run: bool,
                      heartbeat_seconds: int,
                      record_id: Optional[str] = None) -> Path:
        """生成执行器请求。注意：不包含任何账号/口令字段。"""
        body = {
            "attempt_id": attempt.attempt_id,
            "task_key": attempt.task_key,
            "attempt_no": attempt.attempt_no,
            # 租约所有者（父 worker）；执行器必须在结果中原样回显
            "executor_id": attempt.executor_id,
            "record_id": record_id,
            "portal_url": portal_url,
            "form_id": form_id,
            "payload": payload,
            "dry_run": dry_run,
            "heartbeat_seconds": heartbeat_seconds,
            "progress_file": str(self.progress_path(attempt.attempt_id)),
            "result_file": str(self.result_path(attempt.attempt_id)),
            "evidence_dir": str(self.evidence_dir(attempt.attempt_id)),
        }
        path = self.request_path(attempt.attempt_id)
        self._atomic_write_json(path, body)
        return path

    # ----------------------------------------------------------- 检查点读取

    def read_progress(self, attempt_id: str) -> list[dict]:
        path = self.progress_path(attempt_id)
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # 最后一行可能是崩溃瞬间写了一半，忽略半截行即可
                continue
        return events

    def latest_phase(self, attempt_id: str) -> Optional[str]:
        events = self.read_progress(attempt_id)
        for event in reversed(events):
            if isinstance(event, dict) and event.get("phase"):
                return str(event["phase"])
        return None

    # ------------------------------------------------------------- 结果读取

    def result_ready(self, attempt_id: str) -> bool:
        return self.result_path(attempt_id).exists()

    def read_result(self, attempt_id: str, expected_executor: Optional[str] = None
                    ) -> AttemptResult:
        path = self.result_path(attempt_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        required = {"attempt_id", "executor_id", "outcome", "phase_reached"}
        missing = required - set(data)
        if missing:
            raise ValueError(f"result file missing fields: {sorted(missing)}")
        if data["attempt_id"] != attempt_id:
            raise ValueError("result attempt_id does not match file name")
        if expected_executor and data["executor_id"] != expected_executor:
            raise ValueError("result executor_id does not match lease owner")
        if data["outcome"] not in ALLOWED_OUTCOMES:
            raise ValueError(f"unknown outcome: {data['outcome']}")
        return AttemptResult(
            attempt_id=str(data["attempt_id"]),
            executor_id=str(data["executor_id"]),
            outcome=str(data["outcome"]),
            phase_reached=str(data["phase_reached"]),
            portal_ref=data.get("portal_ref") or None,
            detail=str(data.get("detail", "")),
            evidence=list(data.get("evidence", [])),
            dry_run=bool(data.get("dry_run", False)),
            runner_pid=data.get("runner_pid"),
        )

    def remove_result(self, attempt_id: str) -> None:
        try:
            self.result_path(attempt_id).unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------- 证据落盘

    def persist_evidence_bytes(self, attempt_id: str, name: str, data: bytes) -> Path:
        target = self.evidence_dir(attempt_id) / name
        self._atomic_write_bytes(target, data)
        return target

    # ------------------------------------------------------------- 基础工具

    @staticmethod
    def _atomic_write_json(path: Path, body: dict) -> None:
        HandoffSpool._atomic_write_bytes(
            path, json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def result_to_dict(result: AttemptResult) -> dict:
    return asdict(result)
