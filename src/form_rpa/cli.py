"""命令行入口：入队、执行、恢复、查询确认与运营证据视图。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import models
from .applog import configure_logging, get_logger
from .confirmation import (NonQueryablePortalClient, QueryablePortalClient,
                           confirm_task)
from .executor import ExecutorConfig, WorkerExecutor
from .handoff import HandoffSpool
from .inputs import JsonFormSource
from .pipeline import FormPipeline
from .receipts import ReceiptStore
from .reports import queue_overview, render_text, recommended_action, task_dossier
from .security import SecretRedactor

log = get_logger("cli")


def _store(args) -> ReceiptStore:
    return ReceiptStore(args.database, lease_seconds=args.lease_seconds)


def cmd_enqueue(args) -> None:
    store = _store(args)
    try:
        accepted, duplicates = FormPipeline(
            JsonFormSource(args.fixture), store).run()
        print(json.dumps({"accepted": accepted, "duplicates": duplicates,
                          "database": args.database}, ensure_ascii=False))
    finally:
        store.close()


def _build_executor(args, store: ReceiptStore,
                    redactor: SecretRedactor) -> WorkerExecutor:
    config = ExecutorConfig(
        spool_dir=args.spool,
        portal_url=args.portal_url or os.environ.get("RPA_PORTAL_URL", ""),
        form_id=args.form_id or os.environ.get("RPA_FORM_ID", "FORM-DEMO"),
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        run_timeout_seconds=args.run_timeout,
        dry_run=args.dry_run,
        runner=args.runner,
        scenario=args.scenario,
        node_script=args.node_script,
        only_task_key=args.task_key,
    )
    secret_env = {}
    if args.runner == "node":
        # 密钥只经环境变量进子进程，绝不写交接文件或命令行
        for name in ("RPA_USERNAME", "RPA_PASSWORD"):
            value = os.environ.get(name, "")
            if value:
                secret_env[name] = value
                redactor.register(value)
        config.extra_env = secret_env
    return WorkerExecutor(store, config, redactor=redactor,
                          executor_id=args.executor_id)


def cmd_worker(args) -> None:
    redactor = configure_logging()
    store = _store(args)
    try:
        worker = _build_executor(args, store, redactor)
        summaries = worker.run_forever(max_iterations=args.max_tasks)
        print(json.dumps({"processed": summaries, "count": len(summaries)},
                         ensure_ascii=False, indent=2))
    finally:
        store.close()


def cmd_recover(args) -> None:
    configure_logging()
    store = _store(args)
    try:
        events = store.recover_expired()
        print(json.dumps({
            "recovered": [event.__dict__ for event in events],
            "count": len(events)}, ensure_ascii=False, indent=2))
    finally:
        store.close()


def cmd_confirm(args) -> None:
    redactor = configure_logging()
    store = _store(args)
    try:
        if args.no_query:
            client = NonQueryablePortalClient()
        else:
            client = QueryablePortalClient(args.portal_url, redactor=redactor)
        if args.task_key:
            keys = [args.task_key]
        else:
            keys = [row["idempotency_key"] for row in store.list_tasks()
                    if row["state"] == models.UNKNOWN]
        results = []
        for key in keys:
            results.append({"task_key": key,
                            "new_state": confirm_task(store, client, key)})
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        store.close()


def cmd_manual(args) -> None:
    store = _store(args)
    try:
        store.mark_manual(args.task_key, args.note)
        print(json.dumps({"task_key": args.task_key,
                          "state": models.AWAITING_MANUAL}, ensure_ascii=False))
    finally:
        store.close()


def cmd_release(args) -> None:
    store = _store(args)
    try:
        ok = store.release_claim(args.attempt_id, args.executor_id,
                                 reason=args.note)
        print(json.dumps({"attempt_id": args.attempt_id, "released": ok},
                         ensure_ascii=False))
    finally:
        store.close()


def cmd_status(args) -> None:
    store = _store(args)
    try:
        overview = queue_overview(store)
        dossiers = None
        if args.task_key:
            dossiers = [task_dossier(store, args.task_key)]
        elif args.verbose:
            dossiers = [task_dossier(store, row["idempotency_key"])
                        for row in store.list_tasks()]
        if args.json:
            payload = {"overview": overview}
            if dossiers is not None:
                payload["dossiers"] = dossiers
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_text(overview, dossiers))
    finally:
        store.close()


def cmd_spool_clean(args) -> None:
    """清理已结案尝试的交接文件（数据库证据保留）。"""
    store = _store(args)
    spool = HandoffSpool(args.spool)
    removed = 0
    try:
        for task in store.list_tasks():
            if task["state"] not in models.TERMINAL_STATES:
                continue
            for row in store.list_attempts(task["idempotency_key"]):
                for location in (spool.request_path(row["attempt_id"]),
                                 spool.progress_path(row["attempt_id"]),
                                 spool.result_path(row["attempt_id"])):
                    if location.exists():
                        location.unlink()
                        removed += 1
        print(json.dumps({"removed_files": removed}, ensure_ascii=False))
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="form-rpa",
                                     description="表单 RPA 幂等队列与恢复闭环")
    parser.add_argument("--database", default="form-rpa.sqlite3")
    parser.add_argument("--lease-seconds", type=int, default=30)
    sub = parser.add_subparsers(dest="command", required=True)

    p_enqueue = sub.add_parser("enqueue", help="校验并幂等入队")
    p_enqueue.add_argument("--fixture", required=True)
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_worker = sub.add_parser("worker", help="领取并执行任务")
    p_worker.add_argument("--spool", default="spool")
    p_worker.add_argument("--runner", choices=["fake", "node"], default="fake")
    p_worker.add_argument("--node-script", default=None)
    p_worker.add_argument("--scenario", default=None)
    p_worker.add_argument("--portal-url", default=None)
    p_worker.add_argument("--form-id", default=None)
    p_worker.add_argument("--executor-id", default=None)
    p_worker.add_argument("--heartbeat-seconds", type=int, default=5)
    p_worker.add_argument("--run-timeout", type=int, default=120)
    p_worker.add_argument("--max-tasks", type=int, default=None)
    p_worker.add_argument("--task-key", default=None,
                          help="只领取指定任务（定向重试），缺省按队列顺序领取")
    p_worker.add_argument("--dry-run", action="store_true")
    p_worker.set_defaults(func=cmd_worker)

    p_release = sub.add_parser("release", help="释放未结案的领取租约")
    p_release.add_argument("--attempt-id", required=True)
    p_release.add_argument("--executor-id", required=True)
    p_release.add_argument("--note", default="released by operator")
    p_release.set_defaults(func=cmd_release)

    p_recover = sub.add_parser("recover", help="扫描过期租约并推断结论")
    p_recover.set_defaults(func=cmd_recover)

    p_confirm = sub.add_parser("confirm", help="对 unknown 任务做门户查询确认")
    p_confirm.add_argument("--portal-url", default="http://127.0.0.1:8765")
    p_confirm.add_argument("--no-query", action="store_true",
                           help="模拟不支持查询确认的门户")
    p_confirm.add_argument("--task-key", default=None)
    p_confirm.set_defaults(func=cmd_confirm)

    p_manual = sub.add_parser("manual", help="把任务移交人工处理")
    p_manual.add_argument("--task-key", required=True)
    p_manual.add_argument("--note", default="manual takeover")
    p_manual.set_defaults(func=cmd_manual)

    p_status = sub.add_parser("status", help="运营视图：状态与每次尝试证据")
    p_status.add_argument("--task-key", default=None)
    p_status.add_argument("--verbose", action="store_true")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_clean = sub.add_parser("spool-clean", help="清理终态任务的交接文件")
    p_clean.add_argument("--spool", default="spool")
    p_clean.set_defaults(func=cmd_spool_clean)
    return parser


def main(argv=None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
