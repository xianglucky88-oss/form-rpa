"""CLI 端到端：入队、worker、恢复、确认、状态视图的命令行契约。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from form_rpa.cli import main


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(f"/tmp/rpa-cli-{id(self)}")
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = str(self.root / "q.sqlite3")
        self.spool = str(self.root / "spool")
        self.fixture = self.root / "forms.json"
        self.fixture.write_text(json.dumps([
            {"record_id": "CLI-1", "customer_code": "C-1", "amount": 100},
            {"record_id": "CLI-1", "customer_code": "C-1", "amount": 100},
        ]), encoding="utf-8")

    def test_enqueue_worker_confirm_status_flow(self):
        import io
        import contextlib

        def run_cli(*argv):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                main(["--database", self.db, *argv])
            return buffer.getvalue()

        out = run_cli("enqueue", "--fixture", str(self.fixture))
        self.assertEqual(json.loads(out)["accepted"], 1)

        out = run_cli(
            "worker", "--spool", self.spool, "--runner", "fake",
            "--scenario", "portal_timeout", "--max-tasks", "1")
        summary = json.loads(out)
        self.assertEqual(summary["processed"][0]["task_state"], "unknown")

        out = run_cli("recover")
        self.assertEqual(json.loads(out)["count"], 0)

        # 不支持查询的门户：unknown 只能人工
        out = run_cli("confirm", "--no-query")
        self.assertEqual(json.loads(out)[0]["new_state"], "awaiting_manual")

        out = run_cli("status", "--json", "--verbose")
        payload = json.loads(out)
        task = payload["overview"]["tasks"][0]
        self.assertEqual(task["state"], "awaiting_manual")
        dossier = payload["dossiers"][0]
        self.assertEqual(len(dossier["attempts"]), 1)
        self.assertEqual(dossier["attempts"][0]["outcome"], "unknown")
        self.assertTrue(dossier["events"], "事件流必须保留运营审计线索")

    def test_dry_run_cli_keeps_task_pending(self):
        import io
        import contextlib

        def run_cli(*argv):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                main(["--database", self.db, *argv])
            return buffer.getvalue()

        run_cli("enqueue", "--fixture", str(self.fixture))
        run_cli("worker", "--spool", self.spool, "--runner", "fake",
                "--scenario", "ok", "--dry-run", "--max-tasks", "1")
        out = run_cli("status", "--json")
        self.assertEqual(
            json.loads(out)["overview"]["tasks"][0]["state"], "pending")


if __name__ == "__main__":
    unittest.main()
