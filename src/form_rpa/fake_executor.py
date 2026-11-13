"""执行器替身：不启动浏览器，按注入场景复现各类失败/崩溃，供连续恢复演练。

用法（由 executor 子进程方式调用，也可手动）::

    python3 -m form_rpa.fake_executor --request requests/att-xxx.json \
        --scenario login_exit --crash-before-phase pre_click

场景与“是否产生外部动作”的边界严格对应：

    ok                登录 -> 填表 -> 点击 -> 门户确认号 -> 成功
    login_exit        登录前直接退出（无任何外部动作）
    pre_click_fail    登录成功、点击提交前抛错（无外部动作）
    portal_timeout    点击后门户无响应 -> unknown（可能已提交）
    receipt_crash     门户已确认，但写回执前进程被杀 -> unknown 且留有确认号线索
    business_reject   门户明确业务拒绝
    login_failed      凭据错误（无外部动作）
    page_changed      页面结构变化（无外部动作）

崩溃类场景通过 os._exit(2) 模拟 SIGKILL：不写结果文件，只留下阶段检查点。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 阶段事件与 Node 执行器写入同一份 progress jsonl，保证恢复逻辑完全复用
SCENARIOS = {
    "ok", "login_exit", "pre_click_fail", "portal_timeout",
    "receipt_crash", "business_reject", "login_failed", "page_changed",
}


def _append_progress(progress_file: str, event: dict) -> None:
    path = Path(progress_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_result(result_file: str, body: dict) -> None:
    path = Path(result_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True),
                   encoding="utf-8")
    os.replace(tmp, path)


def _crash(progress_file: str, attempt_id: str, executor_id: str, phase: str) -> None:
    _append_progress(progress_file, {
        "attempt_id": attempt_id, "executor_id": executor_id,
        "phase": phase, "event": "crash", "ts": _now(),
    })
    # 硬退出：没有 finally、没有结果文件，等同被 kill -9
    os._exit(2)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run(request: dict, scenario: str, slow: float = 0.0) -> int:
    attempt_id = request["attempt_id"]
    # 租约所有者（父 worker）必须原样回显；本进程 id 仅作诊断
    executor_id = request["executor_id"]
    runner_pid = os.getpid()
    progress_file = request["progress_file"]
    result_file = request["result_file"]
    dry_run = bool(request.get("dry_run"))

    def checkpoint(phase: str, event: str = "phase", **extra) -> None:
        _append_progress(progress_file, {
            "attempt_id": attempt_id, "executor_id": executor_id,
            "runner_pid": runner_pid,
            "phase": phase, "event": event, "ts": _now(), **extra,
        })
        if slow:
            time.sleep(slow)

    def finish(outcome: str, phase: str, **extra) -> int:
        body = {
            "attempt_id": attempt_id, "executor_id": executor_id,
            "runner_pid": runner_pid,
            "outcome": outcome, "phase_reached": phase,
            "dry_run": dry_run, "ts": _now(), **extra,
        }
        _write_result(result_file, body)
        return 0

    if scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario: {scenario}")

    if dry_run:
        # dry-run 只做只读演练：不登录、不点击、不产生成功回执
        checkpoint("pre_click", "dry_run_skip")
        return finish("dry_run", "pre_click",
                      detail="dry-run: no login, no submit, no portal contact")

    # 场景：登录前退出
    if scenario == "login_exit":
        _crash(progress_file, attempt_id, executor_id, "assigned")

    checkpoint("login")
    if scenario == "login_failed":
        return finish("login_failed", "login",
                      detail="portal rejected credentials (no form submitted)")

    checkpoint("pre_click")
    if scenario == "pre_click_fail":
        return finish("failed_pre_action", "pre_click",
                      detail="form page error before submit click")
    if scenario == "page_changed":
        return finish("page_changed", "pre_click",
                      detail="submit button selector no longer matches page")

    # 此后点击已经发生，门户侧可能产生记录
    checkpoint("submitted", event="submit_clicked")
    if scenario == "portal_timeout":
        # 模拟等待门户响应超时：没有确认号，不能声称成功，也不能重试
        _crash(progress_file, attempt_id, executor_id, "submitted")

    # 门户受理，进入确认等待
    checkpoint("awaiting_confirm")
    if scenario == "business_reject":
        return finish("business_failed", "awaiting_confirm",
                      portal_ref=None,
                      detail="portal rejected: amount exceeds profile limit")

    portal_ref = f"PTL-{attempt_id[-8:].upper()}"
    if scenario == "receipt_crash":
        # 门户实际上已确认（ref 写进检查点作为线索），但回执保存前崩溃
        checkpoint("receipt_saving", portal_ref=portal_ref)
        _crash(progress_file, attempt_id, executor_id, "receipt_saving")

    checkpoint("receipt_saving", portal_ref=portal_ref)
    evidence = []
    evidence_dir = request.get("evidence_dir")
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        note = Path(evidence_dir) / "portal_confirmation.txt"
        note.write_text(f"portal_ref={portal_ref}\nconfirmed=true\n", encoding="utf-8")
        evidence.append(str(note))
    return finish("succeeded", "done", portal_ref=portal_ref,
                  detail="portal accepted and returned confirmation",
                  evidence=evidence)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake RPA executor")
    parser.add_argument("--request", required=True)
    parser.add_argument("--scenario", default="ok", choices=sorted(SCENARIOS))
    parser.add_argument("--slow", type=float, default=0.0)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    code = run(request, args.scenario, args.slow)
    sys.exit(code)


if __name__ == "__main__":
    main()
