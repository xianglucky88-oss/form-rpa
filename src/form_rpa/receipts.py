"""SQLite 持久化：任务幂等、领取租约、尝试证据、确认记录与提交顺序。

设计要点
--------
1. 连接以 autocommit 模式打开（isolation_level=None），所有写操作走
   ``_transaction()`` 显式 ``BEGIN IMMEDIATE``，SQLite 写锁保证多执行器/
   多进程之间不会同时把同一任务交给两个执行者。
2. 领取使用条件 UPDATE（WHERE state IN claimable AND lease 已失效），
   以 rowcount 判断是否抢到，而不是“先 SELECT 再 UPDATE”。
3. 结果回传带 attempt_id 与租约归属校验：过期执行器迟到的结果只能登记为
   late 证据，不能覆盖任务后续已经确认的结论。
4. 回执证据先写文件（evidence 目录，fsync），再提交数据库事务；
   数据库里只存证据路径与确认号，避免大对象与敏感值落库。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import models
from .models import (
    ALLOWED_OUTCOMES, CLAIMABLE_STATES, PHASE_ASSIGNED, PHASE_CRASHED,
    POST_ACTION_PHASES, PRE_ACTION_PHASES, TERMINAL_STATES,
    Attempt, AttemptResult, RecoveryEvent,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS submission_tasks (
    idempotency_key   TEXT PRIMARY KEY,
    record_id         TEXT NOT NULL,
    payload           TEXT NOT NULL,
    state             TEXT NOT NULL,
    message           TEXT,
    executor_id       TEXT,
    lease_expires_at  TEXT,
    current_attempt   TEXT,
    attempts_count    INTEGER NOT NULL DEFAULT 0,
    portal_ref        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_attempts (
    attempt_id        TEXT PRIMARY KEY,
    task_key          TEXT NOT NULL REFERENCES submission_tasks(idempotency_key),
    attempt_no        INTEGER NOT NULL,
    executor_id       TEXT NOT NULL,
    dry_run           INTEGER NOT NULL DEFAULT 0,
    phase             TEXT NOT NULL,
    outcome           TEXT,
    portal_ref        TEXT,
    detail            TEXT,
    late              INTEGER NOT NULL DEFAULT 0,
    conflict_note     TEXT,
    evidence_paths    TEXT NOT NULL DEFAULT '[]',
    lease_expires_at  TEXT NOT NULL,
    claimed_at        TEXT NOT NULL,
    phase_updated_at  TEXT NOT NULL,
    result_received_at TEXT,
    finalized_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON submission_attempts(task_key, attempt_no);
CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_no
    ON submission_attempts(task_key, attempt_no);
CREATE TABLE IF NOT EXISTS submission_confirmations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key          TEXT NOT NULL REFERENCES submission_tasks(idempotency_key),
    attempt_id        TEXT,
    portal_ref        TEXT,
    supports_query    INTEGER NOT NULL,
    confirmed         INTEGER,
    note              TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submission_rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   TEXT,
    error       TEXT NOT NULL,
    raw_payload TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS lease_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key    TEXT NOT NULL,
    attempt_id  TEXT NOT NULL,
    executor_id TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
"""

_CLAIMABLE_PLACEHOLDERS = ",".join("?" * len(CLAIMABLE_STATES))


class ReceiptStore:
    """任务、尝试、回执状态和输入拒绝记录的持久化；不执行浏览器动作。"""

    def __init__(self, database: str | Path, lease_seconds: int = 60,
                 clock: Optional[callable] = None) -> None:
        self.database = str(database)
        self.lease_seconds = lease_seconds
        # 可注入时钟（返回 timezone-aware datetime），测试中用来快进租约
        self._clock = clock or utc_now
        Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None -> autocommit；事务全部显式控制
        self.connection = sqlite3.connect(
            self.database, timeout=30, check_same_thread=False,
            isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.executescript(_SCHEMA)

    def _now(self) -> datetime:
        return self._clock()

    @contextlib.contextmanager
    def _transaction(self):
        with self._transaction_on(self.connection) as conn:
            yield conn

    @contextlib.contextmanager
    def _transaction_on(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # ------------------------------------------------------------------ 入队

    def enqueue(self, task) -> bool:
        """幂等入队：同 idempotency_key 已存在则复用，payload 原样保留。"""
        now = iso(self._now())
        with self._transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO submission_tasks"
                "(idempotency_key, record_id, payload, state, message,"
                " attempts_count, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (task.idempotency_key, task.record_id,
                 json.dumps(task.payload, ensure_ascii=False, sort_keys=True),
                 task.state, None, now, now),
            )
            return cursor.rowcount == 1

    def record_rejection(self, record_id: str, raw: object, error: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO submission_rejections(record_id, error, raw_payload)"
                " VALUES (?, ?, ?)",
                (record_id, error,
                 json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)),
            )

    # ------------------------------------------------------------------ 领取

    def claim_next(self, executor_id: str, lease_seconds: Optional[int] = None,
                   dry_run: bool = False,
                   only_task_key: Optional[str] = None) -> Optional[Attempt]:
        """原子领取一个可处理任务。多个执行器并发调用时只有一个能成功。

        only_task_key 非空时只领取指定任务（演练/定向重试用），
        为 None 时按入队顺序领取任意可处理任务。
        """
        ttl = lease_seconds or self.lease_seconds
        now = self._now()
        now_s, expire_s = iso(now), iso(now + timedelta(seconds=ttl))
        extra_filter = "AND idempotency_key = ?" if only_task_key else ""
        extra_params = (only_task_key,) if only_task_key else ()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT idempotency_key, attempts_count FROM submission_tasks"
                f" WHERE state IN ({_CLAIMABLE_PLACEHOLDERS})"
                " AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
                + extra_filter
                + " ORDER BY created_at ASC LIMIT 1",
                (*CLAIMABLE_STATES, now_s, *extra_params),
            ).fetchone()
            if row is None:
                return None
            key, count = row["idempotency_key"], row["attempts_count"]
            attempt_id = f"att-{uuid.uuid4().hex[:16]}"
            attempt_no = count + 1
            updated = conn.execute(
                "UPDATE submission_tasks SET state=?,"
                " executor_id=?, lease_expires_at=?, current_attempt=?,"
                " attempts_count=?, updated_at=?"
                f" WHERE idempotency_key=? AND state IN ({_CLAIMABLE_PLACEHOLDERS})"
                " AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
                + extra_filter,
                (models.CLAIMED, executor_id, expire_s, attempt_id, attempt_no,
                 now_s, key, *CLAIMABLE_STATES, now_s, *extra_params),
            )
            if updated.rowcount != 1:
                # 并发竞争中被其他执行器抢先（同一事务内已加写锁，理论上不会走到）
                return None
            conn.execute(
                "INSERT INTO submission_attempts(attempt_id, task_key, attempt_no,"
                " executor_id, dry_run, phase, lease_expires_at, claimed_at,"
                " phase_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, key, attempt_no, executor_id, int(dry_run),
                 PHASE_ASSIGNED, expire_s, now_s, now_s),
            )
            self._event(conn, key, attempt_id, executor_id, "claimed",
                        f"lease expires {expire_s}")
        return self.get_attempt(attempt_id)

    def release_claim(self, attempt_id: str, executor_id: str,
                      reason: str = "released") -> bool:
        """释放未结案的领取，任务回到 pending 可重新排队；归属不符则拒绝。"""
        now_s = iso(self._now())
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT a.task_key, a.executor_id, a.finalized_at, t.state"
                " FROM submission_attempts a JOIN submission_tasks t"
                " ON t.idempotency_key = a.task_key"
                " WHERE a.attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["executor_id"] != executor_id:
                return False
            if row["finalized_at"] is not None:
                return False
            conn.execute(
                "UPDATE submission_attempts SET finalized_at=?, outcome=?,"
                " phase=?, conflict_note=COALESCE(conflict_note, ?)"
                " WHERE attempt_id=?",
                (now_s, models.OUT_FAILED_PRE_ACTION, models.PHASE_CRASHED,
                 reason, attempt_id))
            conn.execute(
                "UPDATE submission_tasks SET state=?, lease_expires_at=NULL,"
                " executor_id=NULL, current_attempt=NULL, message=?,"
                " updated_at=? WHERE idempotency_key=?",
                (models.PENDING, f"claim released: {reason}", now_s,
                 row["task_key"]))
            self._event(conn, row["task_key"], attempt_id, executor_id,
                        "claim_released", reason)
        return True

    def heartbeat(self, attempt_id: str, executor_id: str,
                  phase: Optional[str] = None,
                  lease_seconds: Optional[int] = None,
                  connection: Optional[sqlite3.Connection] = None,
                  renew_lease: bool = True) -> bool:
        """续租并可同时推进阶段检查点。归属不符时拒绝（返回 False）。

        心跳通常在后台线程调用；SQLite 连接不能跨线程共享事务，因此
        调用方可传入线程专属连接，缺省回退到主连接（单线程场景）。
        renew_lease=False 时只推进阶段、不动租约过期时间。
        """
        ttl = lease_seconds or self.lease_seconds
        now = self._now()
        now_s, expire_s = iso(now), iso(now + timedelta(seconds=ttl))
        conn = connection or self.connection
        with self._transaction_on(conn):
            row = conn.execute(
                "SELECT a.task_key, a.executor_id, a.phase, t.current_attempt,"
                " t.executor_id AS task_owner FROM submission_attempts a"
                " JOIN submission_tasks t ON t.idempotency_key = a.task_key"
                " WHERE a.attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["executor_id"] != executor_id:
                return False
            # 任务已被恢复/重新领取：current_attempt 不再指向本尝试
            if row["current_attempt"] != attempt_id:
                return False
            if phase is not None and phase != row["phase"]:
                conn.execute(
                    "UPDATE submission_attempts SET phase=?, phase_updated_at=?"
                    " WHERE attempt_id=?", (phase, now_s, attempt_id))
                self._event(conn, row["task_key"], attempt_id, executor_id,
                            "phase", f"{row['phase']} -> {phase}")
            if renew_lease:
                conn.execute(
                    "UPDATE submission_attempts SET lease_expires_at=?"
                    " WHERE attempt_id=?", (expire_s, attempt_id))
                conn.execute(
                    "UPDATE submission_tasks SET lease_expires_at=?"
                    " WHERE current_attempt=?", (expire_s, attempt_id))
        return True

    def open_worker_connection(self) -> sqlite3.Connection:
        """供后台心跳线程使用的独立连接（同一数据库文件，WAL 下可并发）。"""
        conn = sqlite3.connect(self.database, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ------------------------------------------------------------- 结果回传

    def record_result(self, result: AttemptResult,
                      evidence_paths: Optional[list[str]] = None) -> dict:
        """回传一次尝试的结果。

        返回字典说明处理方式：
          applied —— 结果已应用到任务状态
          late    —— 执行器租约已过期/归属已变，仅登记迟到证据
          stale   —— 任务已到终态，结果不得覆盖
        """
        if result.outcome not in ALLOWED_OUTCOMES:
            raise ValueError(f"unsupported outcome: {result.outcome}")
        now_s = iso(self._now())
        evidence = json.dumps(evidence_paths or [], ensure_ascii=False)
        with self._transaction() as conn:
            attempt = conn.execute(
                "SELECT * FROM submission_attempts WHERE attempt_id=?",
                (result.attempt_id,)).fetchone()
            if attempt is None:
                raise KeyError(f"unknown attempt: {result.attempt_id}")
            task = conn.execute(
                "SELECT * FROM submission_tasks WHERE idempotency_key=?",
                (attempt["task_key"],)).fetchone()

            owns_now = (task["current_attempt"] == result.attempt_id
                        and task["executor_id"] == result.executor_id)
            terminal = task["state"] in TERMINAL_STATES

            # 无论如何先把尝试证据完整落库（迟到结果也是证据）
            conn.execute(
                "UPDATE submission_attempts SET outcome=?, portal_ref=?,"
                " detail=?, phase=?, evidence_paths=?, result_received_at=?,"
                " finalized_at=? WHERE attempt_id=?",
                (result.outcome, result.portal_ref, result.detail,
                 result.phase_reached, evidence, now_s, now_s,
                 result.attempt_id),
            )

            if terminal:
                self._event(conn, task["idempotency_key"], result.attempt_id,
                            result.executor_id, "result_rejected_terminal",
                            f"task already {task['state']}")
                return {"disposition": "stale", "task_state": task["state"]}

            if not owns_now:
                conn.execute(
                    "UPDATE submission_attempts SET late=1, conflict_note=?"
                    " WHERE attempt_id=?",
                    (f"lease superseded; task now on attempt "
                     f"{task['current_attempt']} ({task['state']})",
                     result.attempt_id))
                self._event(conn, task["idempotency_key"], result.attempt_id,
                            result.executor_id, "late_result",
                            f"outcome={result.outcome} kept as evidence only")
                return {"disposition": "late", "task_state": task["state"]}

            # 归属有效：按结果类型决定任务新状态
            new_state, message = self._state_for_outcome(result)
            conn.execute(
                "UPDATE submission_tasks SET state=?, message=?, portal_ref=?,"
                " lease_expires_at=NULL, updated_at=? WHERE idempotency_key=?",
                (new_state, message, result.portal_ref, now_s,
                 task["idempotency_key"]),
            )
            final_phase = (result.phase_reached if result.dry_run
                           else models.PHASE_DONE)
            conn.execute(
                "UPDATE submission_attempts SET phase=? WHERE attempt_id=?",
                (final_phase, result.attempt_id))
            self._event(conn, task["idempotency_key"], result.attempt_id,
                        result.executor_id, "result_applied",
                        f"outcome={result.outcome} state={new_state}")
        return {"disposition": "applied", "task_state": new_state}

    @staticmethod
    def _state_for_outcome(result: AttemptResult) -> tuple[str, str]:
        mapping = {
            models.OUT_SUCCEEDED: models.SUCCEEDED,
            models.OUT_BUSINESS_FAILED: models.BUSINESS_FAILED,
            models.OUT_LOGIN_FAILED: models.LOGIN_FAILED,
            models.OUT_PAGE_CHANGED: models.PAGE_CHANGED,
            models.OUT_FAILED_PRE_ACTION: models.FAILED_RETRYABLE,
            models.OUT_UNKNOWN: models.UNKNOWN,
            # dry-run 不改变任务，仍可再次真实提交
            models.OUT_DRY_RUN: models.PENDING,
        }
        state = mapping[result.outcome]
        note = result.detail or result.outcome
        if result.portal_ref:
            note = f"{note} | portal_ref={result.portal_ref}"
        return state, note

    # ------------------------------------------------------------- 过期恢复

    def recover_expired(self) -> list[RecoveryEvent]:
        """扫描租约过期但未结案的尝试并逐个推断（见 recover_attempt）。"""
        now_s = iso(self._now())
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT attempt_id FROM submission_attempts"
                " WHERE finalized_at IS NULL AND lease_expires_at < ?",
                (now_s,)).fetchall()
            attempt_ids = [row["attempt_id"] for row in rows]
        events = []
        for attempt_id in attempt_ids:
            event = self.recover_attempt(attempt_id, reason="lease expired")
            if event is not None:
                events.append(event)
        return events

    def recover_attempt(self, attempt_id: str,
                        reason: str = "process lost") -> Optional[RecoveryEvent]:
        """对单个未结案尝试按最后检查点阶段推断结论。

        - 最后阶段在点击前：确定没有外部动作 -> failed_retryable，可重新领取
        - 最后阶段在点击后：可能已提交 -> unknown，禁止自动重试
        已经被新尝试取代的过期尝试只补登 crashed，不动任务状态。
        """
        now_s = iso(self._now())
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT a.*, t.state AS task_state, t.current_attempt"
                " FROM submission_attempts a JOIN submission_tasks t"
                " ON t.idempotency_key = a.task_key"
                " WHERE a.attempt_id=?", (attempt_id,)).fetchone()
            if row is None or row["finalized_at"] is not None:
                return None
            key = row["task_key"]
            old_phase = row["phase"]
            superseded = row["current_attempt"] != row["attempt_id"]
            if old_phase in PRE_ACTION_PHASES:
                outcome = models.OUT_FAILED_PRE_ACTION
                new_state = models.FAILED_RETRYABLE
                note = "executor lost before any external action; safe to retry"
            elif old_phase in POST_ACTION_PHASES:
                outcome = models.OUT_UNKNOWN
                new_state = models.UNKNOWN
                note = ("executor lost after submit click; portal may have"
                        " accepted the form, automatic retry is forbidden")
            else:
                outcome, new_state = models.OUT_UNKNOWN, models.UNKNOWN
                note = f"unrecognized phase {old_phase}; treat as unknown"

            conn.execute(
                "UPDATE submission_attempts SET phase=?, outcome=?,"
                " finalized_at=?, conflict_note=COALESCE(conflict_note, ?),"
                " late=CASE WHEN ? THEN 1 ELSE late END WHERE attempt_id=?",
                (PHASE_CRASHED, outcome, now_s,
                 note if superseded else None, int(superseded), attempt_id))
            self._event(conn, key, attempt_id, row["executor_id"],
                        "lease_expired",
                        f"{reason}; last_phase={old_phase} -> {outcome}")
            event = None
            if not superseded and row["task_state"] not in TERMINAL_STATES:
                conn.execute(
                    "UPDATE submission_tasks SET state=?,"
                    " lease_expires_at=NULL, executor_id=NULL,"
                    " current_attempt=NULL, message=?, updated_at=?"
                    " WHERE idempotency_key=?",
                    (new_state, note, now_s, key))
                event = RecoveryEvent(key, attempt_id, old_phase,
                                      outcome, new_state, note)
            else:
                self._event(conn, key, attempt_id, row["executor_id"],
                            "recovery_skipped",
                            f"task already {row['task_state']}")
        return event

    # ------------------------------------------------------------- 查询确认

    def record_confirmation(self, task_key: str, attempt_id: Optional[str],
                            portal_ref: Optional[str], supports_query: bool,
                            confirmed: Optional[bool], note: str) -> str:
        """登记一次门户查询确认结果，返回任务的新状态。"""
        now_s = iso(self._now())
        with self._transaction() as conn:
            task = conn.execute(
                "SELECT state FROM submission_tasks WHERE idempotency_key=?",
                (task_key,)).fetchone()
            if task is None:
                raise KeyError(f"unknown task: {task_key}")
            conn.execute(
                "INSERT INTO submission_confirmations(task_key, attempt_id,"
                " portal_ref, supports_query, confirmed, note, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (task_key, attempt_id, portal_ref, int(supports_query),
                 None if confirmed is None else int(confirmed), note, now_s))
            if task["state"] in TERMINAL_STATES and task["state"] != models.SUCCEEDED:
                return task["state"]  # 终态不再改写
            if supports_query and confirmed is True:
                new_state = models.SUCCEEDED
                conn.execute(
                    "UPDATE submission_tasks SET state=?,"
                    " portal_ref=COALESCE(?, portal_ref), lease_expires_at=NULL,"
                    " message=?, updated_at=? WHERE idempotency_key=?",
                    (new_state, portal_ref,
                     f"confirmed via portal query: {note}", now_s, task_key))
            elif supports_query and confirmed is False:
                # 门户明确查无此单 => 此前的 unknown 可安全重试
                new_state = models.FAILED_RETRYABLE
                conn.execute(
                    "UPDATE submission_tasks SET state=?, lease_expires_at=NULL,"
                    " message=?, updated_at=? WHERE idempotency_key=?",
                    (new_state, f"portal query confirms no record: {note}",
                     now_s, task_key))
            else:
                # 不支持查询 / 无法判定 -> 人工
                new_state = models.AWAITING_MANUAL
                conn.execute(
                    "UPDATE submission_tasks SET state=?, lease_expires_at=NULL,"
                    " message=?, updated_at=? WHERE idempotency_key=?",
                    (new_state, f"cannot verify via portal: {note}", now_s, task_key))
            self._event(conn, task_key, attempt_id or "-", "ops",
                        "confirmation",
                        f"supports_query={supports_query} confirmed={confirmed}"
                        f" -> {new_state}")
        return new_state

    def mark_manual(self, task_key: str, note: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE submission_tasks SET state=?, lease_expires_at=NULL,"
                " message=?, updated_at=? WHERE idempotency_key=?",
                (models.AWAITING_MANUAL, note, iso(self._now()), task_key))

    # ------------------------------------------------------------- 只读视图

    def get_task(self, key: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM submission_tasks WHERE idempotency_key=?", (key,)).fetchone()

    def get_attempt(self, attempt_id: str) -> Optional[Attempt]:
        row = self.connection.execute(
            "SELECT * FROM submission_attempts WHERE attempt_id=?",
            (attempt_id,)).fetchone()
        if row is None:
            return None
        return Attempt(
            attempt_id=row["attempt_id"], task_key=row["task_key"],
            attempt_no=row["attempt_no"], executor_id=row["executor_id"],
            lease_expires_at=row["lease_expires_at"], phase=row["phase"],
            outcome=row["outcome"], portal_ref=row["portal_ref"],
            evidence_path=row["evidence_paths"],
            result_received_at=row["result_received_at"],
            late=bool(row["late"]), conflict_note=row["conflict_note"],
            finalized_at=row["finalized_at"])

    def list_attempts(self, task_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM submission_attempts WHERE task_key=? ORDER BY attempt_no",
            (task_key,)).fetchall())

    def list_confirmations(self, task_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM submission_confirmations WHERE task_key=? ORDER BY id",
            (task_key,)).fetchall())

    def list_events(self, task_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM lease_events WHERE task_key=? ORDER BY id",
            (task_key,)).fetchall())

    def list_tasks(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM submission_tasks ORDER BY created_at").fetchall())

    def _event(self, conn, task_key: str, attempt_id: str, executor_id: str,
               event: str, detail: str = "") -> None:
        conn.execute(
            "INSERT INTO lease_events(task_key, attempt_id, executor_id,"
            " event, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (task_key, attempt_id, executor_id, event, detail, iso(self._now())))

    def close(self) -> None:
        self.connection.close()
