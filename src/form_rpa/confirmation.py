"""门户查询确认：把 unknown 任务区分为可证实、可安全重试、必须人工。

两类门户
--------
- supports_query=True：门户提供按业务键/确认号查询的接口。
  * 查到确认记录 -> succeeded（补登确认证据）
  * 明确查无记录 -> failed_retryable（可以安全重新提交）
- supports_query=False：门户没有查询能力，unknown 永远无法自动消除
  -> awaiting_manual，由运营人员依据尝试证据决定。

本模块只做只读查询，绝不重新提交表单。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Protocol

from .applog import get_logger
from .models import UNKNOWN
from .receipts import ReceiptStore
from .security import SecretRedactor

log = get_logger("confirm")


class PortalClient(Protocol):
    supports_query: bool

    def query(self, task_key: str, record_id: str,
              portal_ref: Optional[str]
              ) -> tuple[Optional[bool], str, Optional[str]]:
        """返回 (confirmed, note, portal_ref)。

        confirmed: True/False/None（无法判定）；
        portal_ref: 门户查询返回的确认号，查无或不支持时为 None。
        """


class QueryablePortalClient:
    """本地演示门户 / 支持查询的真实门户的只读客户端（stdlib urllib）。"""

    supports_query = True

    def __init__(self, base_url: str, redactor: Optional[SecretRedactor] = None,
                 timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.redactor = redactor or SecretRedactor()
        self.timeout = timeout

    def query(self, task_key: str, record_id: str,
              portal_ref: Optional[str]
              ) -> tuple[Optional[bool], str, Optional[str]]:
        params = {"task_key": task_key, "record_id": record_id}
        if portal_ref:
            params["portal_ref"] = portal_ref
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}/api/query?{query}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            # 查询接口自身故障：不能当作“查无”，必须保持 unknown -> 人工
            log.error("portal query failed", extra={"extra_fields": {
                "task": task_key, "error": self.redactor.redact(str(exc))}})
            return None, f"query endpoint error: {type(exc).__name__}", None
        found = bool(body.get("found"))
        ref = body.get("portal_ref")
        note = f"portal query found={found}" + (f" ref={ref}" if ref else "")
        return found, note, (ref if found else None)


class NonQueryablePortalClient:
    """不支持查询确认的门户：任何 unknown 都只能人工处理。"""

    supports_query = False

    def query(self, task_key: str, record_id: str,
              portal_ref: Optional[str]
              ) -> tuple[Optional[bool], str, Optional[str]]:
        return None, "portal provides no confirmation query endpoint", None


def confirm_task(store: ReceiptStore, client: PortalClient, task_key: str) -> str:
    """对单个任务执行一次查询确认并把结论落库，返回新任务状态。"""
    task = store.get_task(task_key)
    if task is None:
        raise KeyError(f"unknown task: {task_key}")
    confirmed, note, queried_ref = client.query(
        task_key, task["record_id"], task["portal_ref"])
    # 优先使用门户查询返回的确认号，其次保留任务已有的线索
    effective_ref = queried_ref or task["portal_ref"]
    attempts = store.list_attempts(task_key)
    latest_attempt = attempts[-1]["attempt_id"] if attempts else None
    new_state = store.record_confirmation(
        task_key, latest_attempt, effective_ref,
        supports_query=client.supports_query, confirmed=confirmed, note=note)
    log.info("confirmation settled", extra={"extra_fields": {
        "task": task_key, "supports_query": client.supports_query,
        "confirmed": confirmed, "new_state": new_state}})
    return new_state


def confirm_pending_unknowns(store: ReceiptStore,
                             client: PortalClient) -> list[dict]:
    """批量处理所有 unknown 任务。"""
    results = []
    for task in store.list_tasks():
        if task["state"] == UNKNOWN:
            results.append({"task_key": task["idempotency_key"],
                            "new_state": confirm_task(store, client,
                                                      task["idempotency_key"])})
    return results
