"""运营视图：汇总任务状态、每次尝试证据与确认记录，供人工决策。"""
from __future__ import annotations

import json
from typing import Optional

from .receipts import ReceiptStore


def task_dossier(store: ReceiptStore, task_key: str) -> dict:
    """单个任务的完整证据档案：任务、全部尝试、确认记录、事件流。"""
    task = store.get_task(task_key)
    if task is None:
        raise KeyError(f"unknown task: {task_key}")
    attempts = []
    for row in store.list_attempts(task_key):
        attempts.append({
            "attempt_id": row["attempt_id"],
            "attempt_no": row["attempt_no"],
            "executor_id": row["executor_id"],
            "dry_run": bool(row["dry_run"]),
            "phase": row["phase"],
            "outcome": row["outcome"],
            "portal_ref": row["portal_ref"],
            "detail": row["detail"],
            "late": bool(row["late"]),
            "conflict_note": row["conflict_note"],
            "evidence_paths": json.loads(row["evidence_paths"] or "[]"),
            "claimed_at": row["claimed_at"],
            "lease_expires_at": row["lease_expires_at"],
            "result_received_at": row["result_received_at"],
            "finalized_at": row["finalized_at"],
        })
    confirmations = [dict(row) for row in store.list_confirmations(task_key)]
    events = [{"event": row["event"], "detail": row["detail"],
               "executor_id": row["executor_id"], "attempt_id": row["attempt_id"],
               "created_at": row["created_at"]}
              for row in store.list_events(task_key)]
    return {
        "task": {k: task[k] for k in task.keys()},
        "attempts": attempts,
        "confirmations": confirmations,
        "events": events,
    }


def queue_overview(store: ReceiptStore) -> dict:
    tasks = store.list_tasks()
    by_state: dict[str, int] = {}
    rows = []
    for task in tasks:
        by_state[task["state"]] = by_state.get(task["state"], 0) + 1
        rows.append({
            "task_key": task["idempotency_key"],
            "record_id": task["record_id"],
            "state": task["state"],
            "attempts": task["attempts_count"],
            "portal_ref": task["portal_ref"],
            "current_attempt": task["current_attempt"],
            "lease_expires_at": task["lease_expires_at"],
            "message": task["message"],
        })
    return {"total": len(tasks), "by_state": by_state, "tasks": rows}


# 运营动作建议：状态 -> (建议, 说明)
ACTION_HINTS = {
    "pending": ("continue", "等待执行器领取，无需人工动作"),
    "claimed": ("continue", "执行器持有租约中，可观察心跳检查点"),
    "failed_retryable": ("retry", "确认点击前失败，可安全重新领取执行"),
    "login_failed": ("retry", "登录失败且未提交表单，修正凭据后重试"),
    "page_changed": ("retry", "页面结构变化，修复选择器后重试"),
    "unknown": ("query", "可能已提交：先用门户查询确认，禁止直接重提"),
    "awaiting_manual": ("manual", "门户无法查证或查询失败，请人工核对后处置"),
    "succeeded": ("done", "门户已确认成功，保留证据归档"),
    "business_failed": ("manual", "门户明确业务拒绝，需业务侧修正数据"),
}


def recommended_action(state: str) -> dict:
    action, note = ACTION_HINTS.get(state, ("manual", "未知状态，默认人工处理"))
    return {"action": action, "note": note}


def render_text(overview: dict, dossiers: Optional[list[dict]] = None) -> str:
    lines = ["== 队列总览 ==",
             f"任务总数: {overview['total']}",
             "按状态: " + ", ".join(f"{k}={v}" for k, v in
                                    sorted(overview["by_state"].items())),
             "", "== 任务列表 =="]
    for row in overview["tasks"]:
        hint = recommended_action(row["state"])
        lines.append(
            f"- {row['record_id']} [{row['state']}] 尝试 {row['attempts']} 次"
            f" | ref={row['portal_ref'] or '-'} | 建议: {hint['action']} - {hint['note']}"
            + (f" | {row['message']}" if row["message"] else ""))
    for dossier in dossiers or []:
        lines.append("")
        lines.append(f"== 任务证据 {dossier['task']['idempotency_key']} ==")
        for attempt in dossier["attempts"]:
            tag = " (迟到,仅存证)" if attempt["late"] else ""
            lines.append(
                f"  尝试 #{attempt['attempt_no']} {attempt['attempt_id']}{tag}"
                f" 执行器={attempt['executor_id']} dry_run={attempt['dry_run']}"
                f" 阶段={attempt['phase']} 结果={attempt['outcome'] or '-'}")
            if attempt["portal_ref"]:
                lines.append(f"    门户确认号: {attempt['portal_ref']}")
            if attempt["detail"]:
                lines.append(f"    说明: {attempt['detail']}")
            for evidence in attempt["evidence_paths"]:
                lines.append(f"    证据: {evidence}")
            if attempt["conflict_note"]:
                lines.append(f"    冲突备注: {attempt['conflict_note']}")
        for conf in dossier["confirmations"]:
            lines.append(
                f"  查询确认: supports={conf['supports_query']}"
                f" confirmed={conf['confirmed']} note={conf['note']}")
    return "\n".join(lines)
