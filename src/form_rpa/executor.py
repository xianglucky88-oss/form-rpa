"""执行器编排：领取任务 -> 交接文件 -> 启动浏览器/替身进程 ->
心跳续约 -> 结果回传 -> 崩溃即时恢复。

进程模型
--------
- 每个任务由一个子进程执行（Node Playwright 或 fake_executor 替身）。
- 父进程内的心跳线程只做两件事：读取 progress jsonl 的最新阶段并续租，
  不触碰任务状态，因此与 SQLite 单写者约定不冲突（心跳只做归属校验 UPDATE）。
- 子进程异常退出且没有结果文件时，父进程立即调用 recover_attempt，
  依据最后检查点阶段给出 failed_retryable 或 unknown，不依赖人工等待。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import models
from .applog import get_logger
from .handoff import HandoffSpool
from .receipts import ReceiptStore
from .security import SecretRedactor

log = get_logger("executor")


@dataclass
class ExecutorConfig:
    spool_dir: str
    portal_url: str = "http://127.0.0.1:8765/"
    form_id: str = "FORM-DEMO"
    lease_seconds: int = 30
    heartbeat_seconds: int = 5
    run_timeout_seconds: int = 120
    dry_run: bool = False
    # "fake:python -m form_rpa.fake_executor --scenario ok" 或 node 脚本路径
    runner: str = "fake"
    scenario: Optional[str] = None
    node_script: Optional[str] = None
    only_task_key: Optional[str] = None
    extra_env: dict = field(default_factory=dict)


class WorkerExecutor:
    def __init__(self, store: ReceiptStore, config: ExecutorConfig,
                 redactor: Optional[SecretRedactor] = None,
                 executor_id: Optional[str] = None) -> None:
        self.store = store
        self.config = config
        self.spool = HandoffSpool(config.spool_dir)
        self.redactor = redactor or SecretRedactor.from_env()
        self.executor_id = executor_id or f"exec-{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()

    # ------------------------------------------------------------ 单次执行

    def execute_once(self) -> Optional[dict]:
        attempt = self.store.claim_next(
            self.executor_id, lease_seconds=self.config.lease_seconds,
            dry_run=self.config.dry_run,
            only_task_key=self.config.only_task_key)
        if attempt is None:
            return None
        task = self.store.get_task(attempt.task_key)
        payload = json.loads(task["payload"])
        log.info("task claimed", extra={"extra_fields": {
            "task": attempt.task_key, "attempt": attempt.attempt_id,
            "attempt_no": attempt.attempt_no, "dry_run": self.config.dry_run}})

        request_path = self.spool.write_request(
            attempt, self.config.portal_url, self.config.form_id,
            payload, self.config.dry_run, self.config.heartbeat_seconds,
            record_id=task["record_id"])
        command, env = self._build_command(request_path)
        log.info("launching runner", extra={"extra_fields": {
            "attempt": attempt.attempt_id, "command": self.redactor.redact(
                " ".join(shlex.quote(part) for part in command))}})

        process = subprocess.Popen(
            command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            cwd=self.config.spool_dir)
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(attempt.attempt_id, process),
            name=f"hb-{attempt.attempt_id}", daemon=True)
        heartbeat.start()
        try:
            stdout, _ = process.communicate(
                timeout=self.config.run_timeout_seconds)
        except subprocess.TimeoutExpired:
            log.error("runner timeout, terminating",
                      extra={"extra_fields": {"attempt": attempt.attempt_id}})
            process.kill()
            stdout, _ = process.communicate()
        heartbeat.join(timeout=2)
        self._log_runner_output(attempt.attempt_id, stdout)

        return self._settle(attempt.attempt_id, process.returncode)

    # ------------------------------------------------------------ 结果结算

    def _settle(self, attempt_id: str, returncode: int) -> dict:
        # 先把 spool 里的检查点阶段同步回数据库，保证恢复依据是最新的
        phase = self.spool.latest_phase(attempt_id)
        if phase:
            self.store.heartbeat(attempt_id, self.executor_id, phase=phase)

        if self.spool.result_ready(attempt_id):
            result = self.spool.read_result(
                attempt_id, expected_executor=self.executor_id)
            evidence_paths = self._collect_evidence(attempt_id, result)
            disposition = self.store.record_result(result, evidence_paths)
            log.info("result recorded", extra={"extra_fields": {
                "attempt": attempt_id, "outcome": result.outcome,
                "disposition": disposition, "portal_ref": result.portal_ref,
                "dry_run": result.dry_run}})
            return {"attempt_id": attempt_id, **disposition}

        # 没有结果文件：子进程崩溃/被杀。立即按最后阶段推断，不等租约超时
        log.error("runner exited without result file",
                  extra={"extra_fields": {"attempt": attempt_id,
                                          "returncode": returncode,
                                          "last_phase": phase}})
        event = self.store.recover_attempt(
            attempt_id, reason=f"runner exited rc={returncode} without result")
        if event is None:  # 已被其他流程结案
            current = self.store.get_attempt(attempt_id)
            return {"attempt_id": attempt_id, "disposition": "late",
                    "task_state": current.outcome if current else "unknown"}
        return {"attempt_id": attempt_id, "disposition": "recovered",
                "task_state": event.new_task_state,
                "inferred_outcome": event.inferred_outcome, "note": event.note}

    def _collect_evidence(self, attempt_id: str,
                          result: models.AttemptResult) -> list[str]:
        stored = []
        evidence_dir = self.spool.evidence_dir(attempt_id)
        for rel in result.evidence:
            source = Path(rel)
            # 子进程 cwd 与父进程不同：绝对路径直接用，相对路径才基于 evidence_dir
            if not source.is_absolute():
                source = evidence_dir / source
            if source.exists() and source.is_file():
                stored.append(str(source))
            else:
                log.warning("declared evidence missing",
                            extra={"extra_fields": {
                                "attempt": attempt_id, "path": str(source)}})
        return stored

    # ------------------------------------------------------------ 心跳线程

    def _heartbeat_loop(self, attempt_id: str, process: subprocess.Popen) -> None:
        # 后台线程使用独立 SQLite 连接，绝不与主线程共享事务
        conn = self.store.open_worker_connection()
        last_phase = None
        try:
            while process.poll() is None and not self._stop.is_set():
                if self._stop.wait(self.config.heartbeat_seconds):
                    break
                phase = self.spool.latest_phase(attempt_id)
                if phase and phase != last_phase:
                    last_phase = phase
                try:
                    ok = self.store.heartbeat(
                        attempt_id, self.executor_id, phase=phase,
                        lease_seconds=self.config.lease_seconds, connection=conn)
                except sqlite3.Error as exc:
                    log.warning("heartbeat database error",
                                extra={"extra_fields": {
                                    "attempt": attempt_id,
                                    "error": type(exc).__name__}})
                    break
                if not ok:
                    log.warning("heartbeat rejected; lease lost",
                                extra={"extra_fields": {"attempt": attempt_id}})
                    # 租约已被恢复流程接管：终止仍在运行的旧执行器，防止迟到提交
                    try:
                        process.send_signal(signal.SIGTERM)
                    except OSError:
                        pass
                    return
        finally:
            conn.close()

    # ------------------------------------------------------------ 连续循环

    def run_forever(self, idle_seconds: float = 0.2,
                    max_iterations: Optional[int] = None) -> list[dict]:
        """持续领取执行直到队列清空（或达到迭代上限），返回每次结算摘要。"""
        summaries = []
        iterations = 0
        while not self._stop.is_set():
            summary = self.execute_once()
            if summary is None:
                if max_iterations is not None:
                    break
                if self._stop.wait(idle_seconds):
                    break
                continue
            summaries.append(summary)
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
        return summaries

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 子进程构造

    def _build_command(self, request_path: Path) -> tuple[list[str], dict]:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.extra_env.items()})
        if self.config.runner == "fake":
            scenario = self.config.scenario or "ok"
            # fake_executor 只依赖标准库，直接按文件路径启动；
            # 保留 -I 隔离模式（忽略 PYTHONPATH/cwd，防止植入模块），
            # 因此不能用 -m form_rpa.fake_executor
            script = Path(__file__).resolve().parent / "fake_executor.py"
            command = [os.sys.executable, "-I", str(script),
                       "--request", str(request_path), "--scenario", scenario]
            return command, env
        if self.config.runner == "node":
            if not self.config.node_script:
                raise ValueError("node runner requires node_script path")
            command = ["node", self.config.node_script,
                       "--request", str(request_path)]
            return command, env
        raise ValueError(f"unknown runner: {self.config.runner}")

    def _log_runner_output(self, attempt_id: str, raw: bytes | None) -> None:
        if not raw:
            return
        for line in raw.decode("utf-8", errors="replace").splitlines():
            clean = self.redactor.redact(line)
            log.info("runner output", extra={"extra_fields": {
                "attempt": attempt_id, "line": clean}})
