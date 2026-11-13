"""领取归属、租约过期、迟到结果与 SQLite 提交顺序的并发正确性测试。"""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path

from form_rpa import models
from form_rpa.executor import ExecutorConfig, WorkerExecutor
from form_rpa.handoff import HandoffSpool
from form_rpa.models import AttemptResult
from form_rpa.receipts import ReceiptStore

from .helpers import FakeClock, seed_task


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(f"/tmp/rpa-test-{id(self)}")
        self.clock = FakeClock()
        self.store = ReceiptStore(self.root / "q.sqlite3",
                                  lease_seconds=30, clock=self.clock)
        self.task = seed_task(self.store)

    def tearDown(self):
        self.store.close()

    def test_claim_is_atomic_under_concurrency(self):
        winners = []
        barrier = threading.Barrier(8)

        def worker(idx):
            # 生产环境中每个执行器进程持有独立连接；这里模拟同一行为，
            # 多个 store 指向同一个数据库文件，由 SQLite 写锁仲裁
            own_store = ReceiptStore(self.root / "q.sqlite3",
                                     lease_seconds=30, clock=self.clock)
            barrier.wait()
            try:
                attempt = own_store.claim_next(f"exec-{idx}", lease_seconds=30)
                if attempt is not None:
                    winners.append((idx, attempt.attempt_id))
            finally:
                own_store.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1, "同一任务只能被一个执行器领取")
        task = self.store.get_task(self.task.idempotency_key)
        self.assertEqual(task["state"], models.CLAIMED)
        self.assertEqual(task["executor_id"], f"exec-{winners[0][0]}")
        self.assertEqual(task["attempts_count"], 1)

    def test_other_executor_heartbeat_is_rejected(self):
        attempt = self.store.claim_next("exec-A", lease_seconds=30)
        self.assertFalse(
            self.store.heartbeat(attempt.attempt_id, "exec-B", phase="login"))
        self.assertTrue(
            self.store.heartbeat(attempt.attempt_id, "exec-A", phase="login"))

    def test_expired_lease_is_reclaimable_and_keeps_attempt_history(self):
        first = self.store.claim_next("exec-A", lease_seconds=30)
        # 时钟快进超过租约，模拟执行器失联
        self.clock.advance(31)
        events = self.store.recover_expired()
        self.assertEqual(len(events), 1)
        task = self.store.get_task(self.task.idempotency_key)
        self.assertEqual(task["state"], models.FAILED_RETRYABLE)
        second = self.store.claim_next("exec-B", lease_seconds=30)
        self.assertIsNotNone(second)
        self.assertEqual(second.attempt_no, 2)
        attempts = self.store.list_attempts(self.task.idempotency_key)
        self.assertEqual([a["attempt_no"] for a in attempts], [1, 2])
        self.assertEqual(attempts[0]["phase"], models.PHASE_CRASHED)

    def test_late_result_is_kept_as_evidence_without_overwriting(self):
        # A 领取后在点击前崩溃，租约过期
        first = self.store.claim_next("exec-A", lease_seconds=30)
        self.store.heartbeat(first.attempt_id, "exec-A",
                             phase=models.PHASE_PRE_CLICK)
        self.clock.advance(31)
        self.store.recover_expired()
        # B 重新领取（任务仍处于非终态 claimed）
        second = self.store.claim_next("exec-B", lease_seconds=30)

        # A 迟到的“成功”结果：归属已变但任务未终态 -> late，仅存证
        late = AttemptResult(
            attempt_id=first.attempt_id, executor_id="exec-A",
            outcome=models.OUT_SUCCEEDED, phase_reached="done",
            portal_ref="PTL-A-LATE")
        disposition = self.store.record_result(late)
        self.assertEqual(disposition["disposition"], "late")
        task = self.store.get_task(self.task.idempotency_key)
        self.assertEqual(task["state"], models.CLAIMED)
        self.assertIsNone(task["portal_ref"])
        first_row = self.store.get_attempt(first.attempt_id)
        self.assertTrue(first_row.late)
        self.assertIn("superseded", first_row.conflict_note)
        # A 的证据仍然完整可查
        self.assertEqual(first_row.outcome, models.OUT_SUCCEEDED)
        self.assertEqual(first_row.portal_ref, "PTL-A-LATE")

    def test_targeted_claim_and_release_roundtrip(self):
        other = seed_task(self.store, record_id="R-OTHER", amount=7)
        # 队列里有两个任务时，定向领取只返回指定任务
        target_attempt = self.store.claim_next(
            "exec-A", lease_seconds=30, only_task_key=other.idempotency_key)
        self.assertEqual(target_attempt.task_key, other.idempotency_key)
        # 非归属执行器不能释放
        self.assertFalse(
            self.store.release_claim(target_attempt.attempt_id, "exec-X"))
        # 归属执行器释放后任务回到 pending，尝试留痕但不影响幂等键
        self.assertTrue(self.store.release_claim(
            target_attempt.attempt_id, "exec-A", reason="operator cancel"))
        row = self.store.get_task(other.idempotency_key)
        self.assertEqual(row["state"], models.PENDING)
        self.assertIsNone(row["current_attempt"])
        self.assertEqual(row["attempts_count"], 1)
        again = self.store.claim_next(
            "exec-B", lease_seconds=30, only_task_key=other.idempotency_key)
        self.assertEqual(again.task_key, other.idempotency_key)
        self.assertEqual(again.attempt_no, 2)

    def test_terminal_state_rejects_any_late_result(self):
        first = self.store.claim_next("exec-A", lease_seconds=30)
        self.clock.advance(31)
        self.store.recover_expired()
        second = self.store.claim_next("exec-B", lease_seconds=30)
        success = AttemptResult(
            attempt_id=second.attempt_id, executor_id="exec-B",
            outcome=models.OUT_SUCCEEDED, phase_reached="done",
            portal_ref="PTL-B-1")
        self.assertEqual(
            self.store.record_result(success)["disposition"], "applied")

        # A 在任务已确认成功后才回传：终态 -> stale，绝不能覆盖
        late = AttemptResult(
            attempt_id=first.attempt_id, executor_id="exec-A",
            outcome=models.OUT_BUSINESS_FAILED, phase_reached="done")
        self.assertEqual(
            self.store.record_result(late)["disposition"], "stale")
        task = self.store.get_task(self.task.idempotency_key)
        self.assertEqual(task["state"], models.SUCCEEDED)
        self.assertEqual(task["portal_ref"], "PTL-B-1")
        # 迟到尝试仍保留自己的证据行
        first_row = self.store.get_attempt(first.attempt_id)
        self.assertEqual(first_row.outcome, models.OUT_BUSINESS_FAILED)

    def test_separate_store_connections_also_serialize_claims(self):
        """模拟真实多进程：每个执行器用独立连接打开同一数据库文件。"""
        second_store = ReceiptStore(
            self.root / "q.sqlite3", lease_seconds=30, clock=self.clock)
        try:
            a1 = self.store.claim_next("proc-1", lease_seconds=30)
            # 第一个租约未过期时，第二个连接无论如何都领不到同一任务
            self.assertIsNone(
                second_store.claim_next("proc-2", lease_seconds=30,
                                        only_task_key=a1.task_key))
            # 过期恢复后第二个连接才能接管
            self.clock.advance(31)
            self.store.recover_expired()
            a2 = second_store.claim_next("proc-2", lease_seconds=30)
            self.assertEqual(a2.task_key, a1.task_key)
            self.assertEqual(a2.attempt_no, 2)
        finally:
            second_store.close()


class RecoveryClassificationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(f"/tmp/rpa-rec-{id(self)}")
        self.clock = FakeClock()
        self.store = ReceiptStore(self.root / "q.sqlite3",
                                  lease_seconds=30, clock=self.clock)
        self.task = seed_task(self.store)

    def tearDown(self):
        self.store.close()

    def _claim_and_phase(self, phase):
        attempt = self.store.claim_next("exec-A", lease_seconds=30)
        self.assertTrue(self.store.heartbeat(
            attempt.attempt_id, "exec-A", phase=phase,
            renew_lease=False))
        return attempt

    def _expire_and_recover(self, attempt):
        self.clock.advance(31)
        return self.store.recover_attempt(attempt.attempt_id,
                                          reason="lease expired")

    def test_pre_action_phases_become_retryable(self):
        for phase in models.PRE_ACTION_PHASES:
            attempt = self._claim_and_phase(phase)
            event = self._expire_and_recover(attempt)
            self.assertEqual(event.new_task_state, models.FAILED_RETRYABLE)
            self.assertEqual(event.inferred_outcome,
                             models.OUT_FAILED_PRE_ACTION)
            # 恢复后可再次领取
            next_attempt = self.store.claim_next("exec-B", lease_seconds=30)
            self.assertIsNotNone(next_attempt)
            self.clock.advance(31)
            self.store.recover_expired()

    def test_post_action_phases_become_unknown_and_block_retry(self):
        for phase in models.POST_ACTION_PHASES:
            # 每个阶段独立库，避免多任务领取顺序干扰
            root = Path(f"/tmp/rpa-rec-phase-{phase}-{id(self)}")
            store = ReceiptStore(root / "q.sqlite3",
                                 lease_seconds=30, clock=self.clock)
            task = seed_task(store, record_id=f"R-{phase}", amount=5)
            attempt = store.claim_next("exec-A", lease_seconds=30)
            store.heartbeat(attempt.attempt_id, "exec-A", phase=phase,
                            renew_lease=False)
            self.clock.advance(31)
            event = store.recover_attempt(attempt.attempt_id)
            self.assertEqual(event.new_task_state, models.UNKNOWN)
            self.assertEqual(event.inferred_outcome, models.OUT_UNKNOWN)
            row = store.get_task(task.idempotency_key)
            self.assertEqual(row["state"], models.UNKNOWN)
            # unknown 不在可领取集合中，任何执行器都拿不到
            self.assertIsNone(store.claim_next("exec-B", lease_seconds=30))
            store.close()

    def test_recover_expired_sweep_is_idempotent(self):
        attempt = self._claim_and_phase(models.PHASE_SUBMITTED)
        self.clock.advance(31)
        first = self.store.recover_expired()
        self.assertEqual(len(first), 1)
        second = self.store.recover_expired()
        self.assertEqual(second, [])
        row = self.store.get_attempt(attempt.attempt_id)
        self.assertEqual(row.phase, models.PHASE_CRASHED)
        self.assertTrue(row.finalized_at)


class WorkerEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(f"/tmp/rpa-e2e-{id(self)}")
        # worker 走真实墙钟（子进程执行），不注入假时钟
        self.store = ReceiptStore(self.root / "q.sqlite3", lease_seconds=30)

    def tearDown(self):
        self.store.close()

    def _worker(self, scenario, executor_id="exec-1", dry_run=False):
        cfg = ExecutorConfig(
            spool_dir=str(self.root / "spool"), runner="fake",
            scenario=scenario, lease_seconds=30,
            heartbeat_seconds=1, dry_run=dry_run)
        return WorkerExecutor(self.store, cfg, executor_id=executor_id)

    def test_success_attempt_keeps_evidence_and_portal_ref(self):
        task = seed_task(self.store)
        summary = self._worker("ok").run_forever(max_iterations=1)
        self.assertEqual(summary[0]["disposition"], "applied")
        row = self.store.get_task(task.idempotency_key)
        self.assertEqual(row["state"], models.SUCCEEDED)
        self.assertTrue(row["portal_ref"].startswith("PTL-"))
        attempt = self.store.list_attempts(task.idempotency_key)[0]
        evidence = json.loads(attempt["evidence_paths"])
        self.assertTrue(evidence and Path(evidence[0]).exists())

    def test_dry_run_creates_no_success_receipt_and_task_stays_pending(self):
        task = seed_task(self.store)
        self._worker("ok", dry_run=True).run_forever(max_iterations=1)
        row = self.store.get_task(task.idempotency_key)
        self.assertEqual(row["state"], models.PENDING)
        attempt = self.store.list_attempts(task.idempotency_key)[0]
        self.assertTrue(attempt["dry_run"])
        self.assertEqual(attempt["outcome"], models.OUT_DRY_RUN)
        self.assertIsNone(attempt["portal_ref"])
        # dry-run 后仍可真实提交
        self._worker("ok").run_forever(max_iterations=1)
        self.assertEqual(
            self.store.get_task(task.idempotency_key)["state"],
            models.SUCCEEDED)

    def test_login_exit_is_recovered_as_retryable_then_succeeds(self):
        task = seed_task(self.store)
        first = self._worker("login_exit").run_forever(max_iterations=1)
        self.assertEqual(first[0]["disposition"], "recovered")
        self.assertEqual(first[0]["task_state"], models.FAILED_RETRYABLE)
        # 连续恢复：同一个任务再次执行成功，两次尝试都可查
        self._worker("ok", executor_id="exec-2").run_forever(max_iterations=1)
        row = self.store.get_task(task.idempotency_key)
        self.assertEqual(row["state"], models.SUCCEEDED)
        self.assertEqual(row["attempts_count"], 2)
        outcomes = [a["outcome"] for a in
                    self.store.list_attempts(task.idempotency_key)]
        self.assertEqual(outcomes,
                         [models.OUT_FAILED_PRE_ACTION, models.OUT_SUCCEEDED])

    def test_receipt_crash_leaves_unknown_with_portal_ref_clue(self):
        task = seed_task(self.store)
        summary = self._worker("receipt_crash").run_forever(max_iterations=1)
        self.assertEqual(summary[0]["task_state"], models.UNKNOWN)
        attempt = self.store.list_attempts(task.idempotency_key)[0]
        # 检查点里保留了门户确认号线索，供人工/查询使用
        progress = HandoffSpool(self.root / "spool").read_progress(
            attempt["attempt_id"])
        refs = [e.get("portal_ref") for e in progress if e.get("portal_ref")]
        self.assertTrue(refs)

    def test_unknown_task_is_not_reclaimed_by_another_worker(self):
        seed_task(self.store)
        self._worker("portal_timeout").run_forever(max_iterations=1)
        # 队列里只剩 unknown 任务时，新 worker 领取不到任何东西
        self.assertIsNone(
            self._worker("ok", executor_id="exec-other").execute_once())

    def test_heartbeat_kills_runner_after_lease_lost(self):
        """租约被恢复流程接管后，心跳线程必须终止仍在运行的旧执行器，
        防止它继续走到点击提交。"""
        import signal
        import subprocess

        task = seed_task(self.store)
        # 慢替身：固定在登录阶段，留出时间让外部接管租约
        worker = self._worker("login_exit")  # 场景名无关，下面直接起慢进程
        attempt = self.store.claim_next(worker.executor_id, lease_seconds=30)
        request = worker.spool.write_request(
            attempt, "http://portal", "FORM-1",
            json.loads(self.store.get_task(task.idempotency_key)["payload"]),
            dry_run=False, heartbeat_seconds=1,
            record_id=task.record_id)
        slow = (
            "import json,time,sys;"
            "req=json.load(open(sys.argv[1]));"
            "open(req['progress_file'],'w').write('');"
            "time.sleep(30)")
        process = subprocess.Popen(
            [os.sys.executable, "-I", "-c", slow, str(request)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        import threading
        hb = threading.Thread(
            target=worker._heartbeat_loop, args=(attempt.attempt_id, process),
            daemon=True)
        hb.start()
        try:
            # 外部用不同执行器身份抢占：先过期恢复，再让心跳被拒
            self.store.recover_attempt(
                attempt.attempt_id, reason="test takeover")
            process.wait(timeout=10)
            self.assertLess(process.returncode, 0,
                            "旧执行器应被 SIGTERM 终止而非正常退出")
        finally:
            if process.poll() is None:
                process.kill()
            worker.stop()
            hb.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
