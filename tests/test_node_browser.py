"""Node + Playwright 与本地演示门户的端到端集成测试。

环境缺少 node 或 playwright 浏览器时自动跳过（fake_executor 的分类
已由其余测试覆盖，这里专门验证跨语言交接与真实浏览器行为）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import unittest
from pathlib import Path

from form_rpa import models
from form_rpa.demo_portal import serve
from form_rpa.executor import ExecutorConfig, WorkerExecutor
from form_rpa.inputs import JsonFormSource
from form_rpa.pipeline import FormPipeline
from form_rpa.receipts import ReceiptStore

ROOT = Path(__file__).resolve().parents[1]
NODE_SCRIPT = ROOT / "automation" / "executor.mjs"


def _browser_lib_path() -> str | None:
    """定位本地解压的 Chromium 依赖库目录（容器无 root 安装时使用）。

    优先环境变量 CHROME_LIB_DIR；其次探测 /tmp/chromelibs。
    """
    candidates = []
    env_dir = os.environ.get("CHROME_LIB_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path("/tmp/chromelibs"))
    for base in candidates:
        libs = [p for p in base.rglob("*.so*") if p.is_file()]
        if libs:
            dirs = sorted({str(p.parent) for p in libs})
            return ":".join(dirs)
    return None


def _node_available() -> bool:
    if not shutil.which("node") or not NODE_SCRIPT.exists():
        return False
    if not (ROOT / "automation" / "node_modules" / "playwright").exists():
        return False
    return _browser_lib_path() is not None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(_node_available(), "node/playwright not available")
class NodeBrowserIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(f"/tmp/rpa-node-{id(self)}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.port = _free_port()
        self.state_file = self.root / "portal.json"
        self.server = serve(
            port=self.port, state_file=str(self.state_file),
            queryable=True, username="demo-user", password="s3cret-pass",
            reject_amount_over=1_000_000)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"
        # 等待门户就绪
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.5):
                    break
            except OSError:
                time.sleep(0.1)
        self.db = self.root / "q.sqlite3"
        self.store = ReceiptStore(self.db, lease_seconds=60)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.store.close()

    def _enqueue(self, record_id, amount=100):
        fixture = self.root / f"{record_id}.json"
        fixture.write_text(json.dumps([
            {"record_id": record_id, "customer_code": "C-1",
             "amount": amount}]), encoding="utf-8")
        return FormPipeline(JsonFormSource(fixture), self.store).run()

    def _node_worker(self, scenario=None, dry_run=False,
                     executor_id="node-worker"):
        env = {"RPA_USERNAME": "demo-user",
               "RPA_PASSWORD": "s3cret-pass",
               "RPA_SCENARIO": scenario or "ok"}
        lib_path = _browser_lib_path()
        if lib_path:
            env["LD_LIBRARY_PATH"] = lib_path
        cfg = ExecutorConfig(
            spool_dir=str(self.root / "spool"), runner="node",
            node_script=str(NODE_SCRIPT), scenario=scenario,
            portal_url=self.base + "/", form_id="FORM-DEMO",
            lease_seconds=60, heartbeat_seconds=3,
            run_timeout_seconds=60, dry_run=dry_run,
            extra_env=env)
        return WorkerExecutor(self.store, cfg, executor_id=executor_id)

    def test_real_browser_success_writes_confirmation(self):
        accepted, _ = self._enqueue("R-BROWSER-OK")
        self.assertEqual(accepted, 1)
        summary = self._node_worker().run_forever(max_iterations=1)
        self.assertEqual(summary[0]["disposition"], "applied")
        task = self.store.list_tasks()[0]
        self.assertEqual(task["state"], models.SUCCEEDED)
        self.assertTrue(task["portal_ref"].startswith("PTL-"))
        # 门户状态文件里确实存在这条受理记录
        portal_records = json.loads(self.state_file.read_text())
        self.assertIn("R-BROWSER-OK", portal_records)
        # 截图证据落盘
        attempt = self.store.list_attempts(task["idempotency_key"])[0]
        evidence = json.loads(attempt["evidence_paths"])
        self.assertTrue(any(Path(p).suffix == ".png" for p in evidence))

    def test_real_browser_dry_run_submits_nothing(self):
        self._enqueue("R-BROWSER-DRY")
        before = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self._node_worker(dry_run=True).run_forever(max_iterations=1)
        task = self.store.list_tasks()[0]
        self.assertEqual(task["state"], models.PENDING)
        after = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.assertEqual(before, after, "dry-run 不得在门户产生任何记录")

    def test_real_browser_business_rejection_is_terminal(self):
        self._enqueue("R-BROWSER-REJECT", amount=99_000_000)
        self._node_worker().run_forever(max_iterations=1)
        task = self.store.list_tasks()[0]
        self.assertEqual(task["state"], models.BUSINESS_FAILED)
        # 业务失败不可被重新领取
        self.assertIsNone(self.store.claim_next("exec-x", lease_seconds=30))

    def test_real_browser_portal_timeout_becomes_unknown(self):
        self._enqueue("R-BROWSER-TIMEOUT")
        env = {"RPA_USERNAME": "demo-user",
               "RPA_PASSWORD": "s3cret-pass",
               "RPA_SCENARIO": "portal_timeout",
               "RPA_STEP_TIMEOUT_MS": "4000"}
        lib_path = _browser_lib_path()
        if lib_path:
            env["LD_LIBRARY_PATH"] = lib_path
        # timeoutMs 设小，门户延迟由执行器按场景注入
        cfg = ExecutorConfig(
            spool_dir=str(self.root / "spool"), runner="node",
            node_script=str(NODE_SCRIPT), scenario="portal_timeout",
            portal_url=self.base + "/", form_id="FORM-DEMO",
            lease_seconds=60, heartbeat_seconds=2,
            run_timeout_seconds=60,
            extra_env=env)
        WorkerExecutor(self.store, cfg, executor_id="node-to").run_forever(
            max_iterations=1)
        task = self.store.list_tasks()[0]
        self.assertEqual(task["state"], models.UNKNOWN)

    def test_credentials_never_reach_logs_or_handoff_files(self):
        import io
        import logging as pylogging
        from form_rpa.applog import RedactingFormatter
        from form_rpa.security import SecretRedactor

        buffer = io.StringIO()
        handler = pylogging.StreamHandler(buffer)
        redactor = SecretRedactor(["s3cret-pass", "demo-user"])
        handler.setFormatter(RedactingFormatter(redactor))
        logger = pylogging.getLogger("form_rpa")
        logger.addHandler(handler)
        try:
            self._enqueue("R-BROWSER-SECRET")
            self._node_worker().run_forever(max_iterations=1)
        finally:
            logger.removeHandler(handler)
        logs = buffer.getvalue()
        self.assertNotIn("s3cret-pass", logs)
        # 交接请求文件也不含口令
        for request_file in (self.root / "spool" / "requests").glob("*.json"):
            self.assertNotIn("s3cret-pass", request_file.read_text())
        # 数据库同样不存口令
        import sqlite3
        con = sqlite3.connect(self.db)
        for table in ("submission_tasks", "submission_attempts",
                      "submission_confirmations", "lease_events"):
            for row in con.execute(f"SELECT * FROM {table}"):
                self.assertNotIn("s3cret-pass", str(row))
        con.close()


if __name__ == "__main__":
    unittest.main()
