#!/usr/bin/env python3
"""端到端连续恢复演练（无需浏览器，使用执行器替身 + 本地演示门户）。

覆盖的场景链
------------
1. 登录前退出 -> 判定无外部动作 -> 自动重试成功（同一任务两次尝试留证）
2. 点击前失败 -> failed_retryable，可安全重试
3. 门户接收后超时 -> unknown，禁止自动重提
4. 保存回执时崩溃 -> unknown，但检查点保留门户确认号线索
5. 查询确认三种结局：
   a. 支持查询且门户有记录 -> succeeded
   b. 支持查询但门户查无记录 -> failed_retryable（可安全重提）
   c. 不支持查询 -> awaiting_manual（人工接管）
6. dry-run 不产生任何门户记录或成功回执，任务保持 pending
7. 8 个执行器并发领取同一任务，只有一个成功
8. 租约过期（注入时钟快进）后任务可被重新领取，历史尝试完整保留

运行：PYTHONPATH=src python3 scripts/demo_recovery.py
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from form_rpa import models  # noqa: E402
from form_rpa.applog import configure_logging  # noqa: E402
from form_rpa.confirmation import (NonQueryablePortalClient,  # noqa: E402
                                   QueryablePortalClient, confirm_task)
from form_rpa.demo_portal import serve  # noqa: E402
from form_rpa.executor import ExecutorConfig, WorkerExecutor  # noqa: E402
from form_rpa.inputs import JsonFormSource  # noqa: E402
from form_rpa.pipeline import FormPipeline  # noqa: E402
from form_rpa.queue import create_task  # noqa: E402
from form_rpa.receipts import ReceiptStore  # noqa: E402
from form_rpa.reports import queue_overview, render_text, task_dossier  # noqa: E402
from form_rpa.validate import validate_record  # noqa: E402
from form_rpa.clock import FakeClock  # noqa: E402

WORK = Path("/tmp/form-rpa-demo")


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def make_worker(store, spool, scenario, executor_id, dry_run=False,
                only_task_key=None):
    cfg = ExecutorConfig(
        spool_dir=str(spool), runner="fake", scenario=scenario,
        lease_seconds=30, heartbeat_seconds=1, dry_run=dry_run,
        only_task_key=only_task_key)
    return WorkerExecutor(store, cfg, executor_id=executor_id)


def key_of(store, record_id: str) -> str:
    for task in store.list_tasks():
        if task["record_id"] == record_id:
            return task["idempotency_key"]
    raise KeyError(record_id)


def run_task(store, spool, record_id, scenario, executor_id, dry_run=False):
    worker = make_worker(store, spool, scenario, executor_id, dry_run,
                         only_task_key=key_of(store, record_id))
    return worker.run_forever(max_iterations=1)


def start_portal(port: int, queryable: bool, state_file: Path):
    server = serve(port=port, state_file=str(state_file),
                   queryable=queryable, username="demo-user",
                   password="s3cret-pass", reject_amount_over=1_000_000)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def portal_submit(base: str, record_id: str, amount: int = 100) -> dict:
    body = json.dumps({"record_id": record_id, "form_id": "FORM-DEMO",
                       "amount": amount}).encode()
    req = urllib.request.Request(
        f"{base}/api/submit", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    configure_logging()
    shutil.rmtree(WORK, ignore_errors=True)
    db = WORK / "queue.sqlite3"
    spool = WORK / "spool"
    store = ReceiptStore(db, lease_seconds=30)

    records = [
        {"record_id": "T1-LOGIN-EXIT", "customer_code": "C-1", "amount": 100},
        {"record_id": "T2-PRE-CLICK", "customer_code": "C-2", "amount": 200},
        {"record_id": "T3-PORTAL-TIMEOUT", "customer_code": "C-3", "amount": 300},
        {"record_id": "T4-RECEIPT-CRASH", "customer_code": "C-4", "amount": 400},
        {"record_id": "T5-NO-QUERY", "customer_code": "C-5", "amount": 500},
        {"record_id": "T6-DRY-RUN", "customer_code": "C-6", "amount": 600},
        {"record_id": "T7-OK", "customer_code": "C-7", "amount": 700},
        {"record_id": "T8-CONCURRENT", "customer_code": "C-8", "amount": 800},
    ]
    fixture = WORK / "forms.json"
    fixture.write_text(json.dumps(records), encoding="utf-8")
    accepted, duplicates = FormPipeline(JsonFormSource(fixture), store).run()
    banner("0. 幂等入队")
    print(f"accepted={accepted} duplicates={duplicates}")

    banner("1. 登录前退出：无外部动作，恢复后自动重试成功")
    run_task(store, spool, "T1-LOGIN-EXIT", "login_exit", "exec-1")
    key1 = key_of(store, "T1-LOGIN-EXIT")
    print("首次尝试后状态:", store.get_task(key1)["state"])
    run_task(store, spool, "T1-LOGIN-EXIT", "ok", "exec-1b")
    print("重试后状态:", store.get_task(key1)["state"],
          "尝试次数:", store.get_task(key1)["attempts_count"])

    banner("2. 点击前失败：可安全重试的技术失败")
    run_task(store, spool, "T2-PRE-CLICK", "pre_click_fail", "exec-2")
    print("T2 状态:", store.get_task(key_of(store, "T2-PRE-CLICK"))["state"],
          "(应为 failed_retryable)")

    banner("3. 门户接收后超时：可能已提交，标记 unknown 并禁止重提")
    run_task(store, spool, "T3-PORTAL-TIMEOUT", "portal_timeout", "exec-3")
    t3 = store.get_task(key_of(store, "T3-PORTAL-TIMEOUT"))
    print("T3 状态:", t3["state"], "(应为 unknown)")
    print("unknown 是否在可领取状态集合中:",
          t3["state"] in models.CLAIMABLE_STATES, "(应为 False)")
    sneak = store.claim_next(
        "exec-sneak", lease_seconds=30, only_task_key=t3["idempotency_key"])
    print("定向领取 unknown 任务结果:", sneak, "(应为 None)")

    banner("4. 保存回执时崩溃：unknown + 检查点中的门户确认号线索")
    run_task(store, spool, "T4-RECEIPT-CRASH", "receipt_crash", "exec-4")
    t4_attempt = store.list_attempts(key_of(store, "T4-RECEIPT-CRASH"))[0]
    from form_rpa.handoff import HandoffSpool
    progress = HandoffSpool(spool).read_progress(t4_attempt["attempt_id"])
    refs = [e.get("portal_ref") for e in progress if e.get("portal_ref")]
    print("T4 状态:", store.get_task(key_of(store, "T4-RECEIPT-CRASH"))["state"],
          "| 检查点保留的确认号线索:", refs)

    banner("5. 门户查询确认：支持查询(有/无记录) 与 不支持查询")
    q_portal, _ = start_portal(8911, True, WORK / "portal-queryable.json")
    nq_portal, _ = start_portal(8912, False, WORK / "portal-nonqueryable.json")
    try:
        # 5a 超时单：门户确实受理过 -> 查询确认成功
        portal_submit("http://127.0.0.1:8911", "T3-PORTAL-TIMEOUT", 300)
        state = confirm_task(
            store, QueryablePortalClient("http://127.0.0.1:8911"),
            key_of(store, "T3-PORTAL-TIMEOUT"))
        print("T3 门户有记录 ->", state)
        # 5b 回执崩溃单：门户查无记录 -> 可安全重试
        state = confirm_task(
            store, QueryablePortalClient("http://127.0.0.1:8911"),
            key_of(store, "T4-RECEIPT-CRASH"))
        print("T4 门户查无记录 ->", state)
        # 5c T5 点击后失联，且门户不支持查询 -> 人工接管
        t5_key = key_of(store, "T5-NO-QUERY")
        attempt = store.claim_next("exec-5", lease_seconds=30,
                                   only_task_key=t5_key)
        store.heartbeat(attempt.attempt_id, "exec-5",
                        phase=models.PHASE_SUBMITTED, renew_lease=False)
        store.recover_attempt(attempt.attempt_id)
        state = confirm_task(store, NonQueryablePortalClient(), t5_key)
        print("T5 门户不支持查询 ->", state)
    finally:
        q_portal.shutdown(); q_portal.server_close()
        nq_portal.shutdown(); nq_portal.server_close()

    banner("6. dry-run：无真实提交、无成功回执")
    run_task(store, spool, "T6-DRY-RUN", "ok", "exec-6", dry_run=True)
    print("T6 dry-run 后状态:",
          store.get_task(key_of(store, "T6-DRY-RUN"))["state"], "(应为 pending)")

    banner("7. 正常成功路径")
    run_task(store, spool, "T7-OK", "ok", "exec-7")
    print("T7 状态:", store.get_task(key_of(store, "T7-OK"))["state"])

    banner("8. 并发领取：8 个执行器竞争同一任务")
    t8_key = key_of(store, "T8-CONCURRENT")
    winners = []
    barrier = threading.Barrier(8)

    def contend(idx):
        own = ReceiptStore(db, lease_seconds=30)
        barrier.wait()
        attempt = own.claim_next(f"racer-{idx}", lease_seconds=30,
                                 only_task_key=t8_key)
        if attempt:
            winners.append((idx, attempt.attempt_id, attempt.task_key))
        own.close()

    threads = [threading.Thread(target=contend, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"获胜者数量: {len(winners)} (应为 1)，获胜执行器: racer-{winners[0][0]}")
    assert len(winners) == 1 and winners[0][2] == t8_key
    # 演示释放租约：获胜者主动放弃后任务重新可领取，再由 exec-8 正常结案
    released = store.release_claim(
        winners[0][1], f"racer-{winners[0][0]}", reason="demo release after race")
    print("获胜者释放租约:", released, "(应为 True)")
    run_task(store, spool, "T8-CONCURRENT", "ok", "exec-8")
    print("T8 最终状态:", store.get_task(t8_key)["state"])

    banner("9. 租约过期恢复（注入时钟，无需真实等待）")
    clock_store = ReceiptStore(WORK / "lease-demo.sqlite3",
                               lease_seconds=30, clock=FakeClock())
    lease_record = validate_record(
        {"record_id": "T9-LEASE", "customer_code": "C-9", "amount": 900})
    lease_task_obj = create_task(lease_record)
    clock_store.enqueue(lease_task_obj)
    first = clock_store.claim_next("exec-slow", lease_seconds=30)
    clock_store.heartbeat(first.attempt_id, "exec-slow",
                          phase=models.PHASE_LOGIN, renew_lease=False)
    clock_store._clock.advance(31)
    events = clock_store.recover_expired()
    print("过期推断:", events[0].inferred_outcome, "->", events[0].new_task_state)
    second = clock_store.claim_next("exec-new", lease_seconds=30)
    print(f"重新领取成功: {second is not None}，新尝试号: {second.attempt_no}")
    history = [a["attempt_no"] for a in
               clock_store.list_attempts(lease_task_obj.idempotency_key)]
    print("尝试历史（每次单独留证）:", history)
    clock_store.close()

    banner("10. 运营证据视图（任务状态 + 每次尝试 + 确认记录）")
    overview = queue_overview(store)
    dossiers = [task_dossier(store, t["idempotency_key"])
                for t in store.list_tasks()]
    print(render_text(overview, dossiers))
    store.close()
    print(f"\n数据库: {db}\n交接目录: {spool}")


if __name__ == "__main__":
    main()
