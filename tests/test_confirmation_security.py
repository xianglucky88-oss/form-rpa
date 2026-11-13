"""门户查询确认（支持/不支持两种门户）与敏感值脱敏测试。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from pathlib import Path

from form_rpa import models
from form_rpa.confirmation import (NonQueryablePortalClient,
                                   QueryablePortalClient, confirm_task)
from form_rpa.demo_portal import serve
from form_rpa.receipts import ReceiptStore
from form_rpa.security import REDACTION, SecretRedactor

from .helpers import FakeClock, seed_task


class PortalServerTestBase(unittest.TestCase):
    queryable = True

    def setUp(self):
        self.root = Path(f"/tmp/rpa-portal-{id(self)}")
        self.state_file = self.root / "portal-state.json"
        self.server = serve(
            port=0, state_file=str(self.state_file),
            queryable=self.queryable, username="demo-user",
            password="s3cret-pass", reject_amount_over=1_000_000)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        self.clock = FakeClock()
        self.store = ReceiptStore(self.root / "q.sqlite3", clock=self.clock)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    def _make_unknown(self, record_id="R-1"):
        task = seed_task(self.store, record_id=record_id)
        attempt = self.store.claim_next("exec-A", lease_seconds=30)
        self.store.heartbeat(attempt.attempt_id, "exec-A",
                             phase=models.PHASE_SUBMITTED,
                             renew_lease=False)
        self.clock.advance(31)
        self.store.recover_expired()
        self.assertEqual(
            self.store.get_task(task.idempotency_key)["state"],
            models.UNKNOWN)
        return task

    def _portal_submit(self, record_id, amount=100):
        body = json.dumps({"record_id": record_id, "form_id": "F1",
                           "amount": amount}).encode()
        req = urllib.request.Request(
            f"{self.base}/api/submit", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())


class QueryablePortalTests(PortalServerTestBase):
    queryable = True

    def test_unknown_confirmed_by_query_becomes_succeeded(self):
        task = self._make_unknown("R-CONFIRM")
        portal = self._portal_submit("R-CONFIRM")
        new_state = confirm_task(
            self.store, QueryablePortalClient(self.base),
            task.idempotency_key)
        self.assertEqual(new_state, models.SUCCEEDED)
        row = self.store.get_task(task.idempotency_key)
        self.assertEqual(row["portal_ref"], portal["portal_ref"])
        confirmations = self.store.list_confirmations(task.idempotency_key)
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(confirmations[0]["confirmed"], 1)

    def test_unknown_with_no_portal_record_becomes_retryable(self):
        task = self._make_unknown("R-MISSING")
        new_state = confirm_task(
            self.store, QueryablePortalClient(self.base),
            task.idempotency_key)
        # 门户明确查无此单：可以安全重试
        self.assertEqual(new_state, models.FAILED_RETRYABLE)
        self.assertIsNotNone(self.store.claim_next("exec-B", lease_seconds=30))

    def test_query_endpoint_failure_keeps_manual_path(self):
        task = self._make_unknown("R-DOWN")
        # 指向一个已关闭端口模拟查询接口故障
        client = QueryablePortalClient("http://127.0.0.1:1", timeout=0.5)
        new_state = confirm_task(self.store, client, task.idempotency_key)
        self.assertEqual(new_state, models.AWAITING_MANUAL)


class NonQueryablePortalTests(PortalServerTestBase):
    queryable = False

    def test_unknown_cannot_be_cleared_without_query_support(self):
        task = self._make_unknown("R-NOQUERY")
        # 门户确实受理过，但没有查询接口
        self._portal_submit("R-NOQUERY")
        new_state = confirm_task(
            self.store, NonQueryablePortalClient(), task.idempotency_key)
        self.assertEqual(new_state, models.AWAITING_MANUAL)
        row = self.store.get_task(task.idempotency_key)
        self.assertEqual(row["state"], models.AWAITING_MANUAL)
        confirmations = self.store.list_confirmations(task.idempotency_key)
        self.assertEqual(confirmations[0]["supports_query"], 0)
        self.assertIsNone(confirmations[0]["confirmed"])
        # 人工接管状态不可被领取
        self.assertIsNone(self.store.claim_next("exec-X", lease_seconds=30))


class RedactionTests(unittest.TestCase):
    def test_registered_secret_values_never_appear(self):
        redactor = SecretRedactor(["s3cret-pass", "tok-abc-123"])
        text = "login with password=s3cret-pass and bearer tok-abc-123 ok"
        clean = redactor.redact(text)
        self.assertNotIn("s3cret-pass", clean)
        self.assertNotIn("tok-abc-123", clean)
        self.assertIn(REDACTION, clean)

    def test_generic_patterns_redact_even_without_registration(self):
        redactor = SecretRedactor()
        clean = redactor.redact(
            "url=https://svc:hunter2@portal.example/form token=zzz")
        self.assertNotIn("hunter2", clean)
        self.assertNotIn("zzz", clean)

    def test_mapping_redacts_sensitive_keys(self):
        redactor = SecretRedactor()
        clean = redactor.redact_mapping({
            "username": "demo-user", "password": "s3cret-pass",
            "nested": {"api_key": "key-9"}})
        self.assertEqual(clean["username"], "demo-user")
        self.assertEqual(clean["password"], REDACTION)
        self.assertEqual(clean["nested"]["api_key"], REDACTION)

    def test_request_file_contains_no_credentials(self):
        # 交接请求结构里没有任何密码字段
        from form_rpa.executor import ExecutorConfig  # noqa: F401
        from form_rpa.handoff import HandoffSpool
        from tests.helpers import make_task
        root = Path(f"/tmp/rpa-redact-{id(self)}")
        store = ReceiptStore(root / "q.sqlite3")
        task = seed_task(store)
        attempt = store.claim_next("exec-1", lease_seconds=30)
        spool = HandoffSpool(root / "spool")
        payload = json.loads(store.get_task(task.idempotency_key)["payload"])
        spool.write_request(attempt, "http://portal", "FORM-1", payload,
                            dry_run=False, heartbeat_seconds=5)
        raw = spool.request_path(attempt.attempt_id).read_text()
        self.assertNotIn("s3cret", raw)
        for forbidden in ("password", "secret", "token", "credential"):
            self.assertNotIn(forbidden, raw.lower())
        store.close()


if __name__ == "__main__":
    unittest.main()
