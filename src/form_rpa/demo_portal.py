"""本地演示门户（仅 stdlib）：模拟表单登录、提交、超时与查询确认。

两种模式
--------
- queryable（默认）：提交记录落盘到 --state 文件，/api/query 可按
  record_id / portal_ref 查证，用于演示 unknown -> succeeded/retryable。
- non-queryable：提交照常受理，但没有 /api/query（返回 404），
  用于演示 unknown 无法消除、只能 awaiting_manual。

页面路径
--------
/                 登录页（Username/Password + Sign in）
/form             表单页（Form ID + Submit）
/api/submit       表单提交 API（支持 delaySeconds 制造点击后超时）
/api/query        查询确认（仅 queryable 模式）
/health           健康检查

注意：这是本地演练用的假门户，账号口令只从启动参数读取且不写日志。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HTML_LOGIN = """<!doctype html><html><head><meta charset="utf-8">
<title>Demo Portal - Login</title></head><body>
<h1>演示门户登录</h1>
<form method="post" action="/login">
<label>Username <input name="username" aria-label="Username"></label><br>
<label>Password <input name="password" type="password" aria-label="Password"></label><br>
<button type="submit">Sign in</button>
</form></body></html>"""

HTML_FORM = """<!doctype html><html><head><meta charset="utf-8">
<title>Demo Portal - Form</title></head><body>
<h1>业务表单提交</h1>
<form method="post" action="/api/submit">
<label>Form ID <input name="form_id" aria-label="Form ID" value="{form_id}"></label><br>
<label>Record ID <input name="record_id" aria-label="Record ID"></label><br>
<label>Amount <input name="amount" aria-label="Amount"></label><br>
<label>响应延迟秒 <input name="delay_seconds" value="0"></label><br>
<button type="submit">Submit</button>
</form><p><a href="/api/query?record_id=demo">查询接口自检</a></p>
</body></html>"""


class PortalState:
    def __init__(self, state_file: str | None) -> None:
        self.lock = threading.Lock()
        self.state_file = Path(state_file) if state_file else None
        self.records: dict[str, dict] = {}
        if self.state_file and self.state_file.exists():
            try:
                self.records = json.loads(
                    self.state_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.records = {}

    def persist(self) -> None:
        if not self.state_file:
            return
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.records, ensure_ascii=False, indent=2,
                                  sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def add(self, record: dict) -> None:
        with self.lock:
            self.records[record["record_id"]] = record
            self.persist()

    def find(self, record_id: str | None, portal_ref: str | None) -> dict | None:
        with self.lock:
            if portal_ref:
                for record in self.records.values():
                    if record.get("portal_ref") == portal_ref:
                        return dict(record)
            if record_id and record_id in self.records:
                return dict(self.records[record_id])
        return None


def make_handler(state: PortalState, username: str, password: str,
                 queryable: bool, reject_amount_over: int):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DemoPortal/1.0"

        def log_message(self, fmt, *args):  # 覆盖默认日志：不打印查询串中的敏感字段
            sys.stderr.write("[portal] %s %s\n" % (self.command, self.path.split("?")[0]))

        def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict):
            self._send(code, json.dumps(payload).encode("utf-8"),
                       "application/json")

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self._json(200, {"ok": True, "queryable": queryable})
            if parsed.path == "/":
                return self._send(200, HTML_LOGIN.encode("utf-8"))
            if parsed.path == "/form":
                return self._send(200, HTML_FORM.format(
                    form_id=os.environ.get("RPA_FORM_ID", "FORM-DEMO")).encode("utf-8"))
            if parsed.path == "/api/query":
                if not queryable:
                    return self._json(404, {"error": "query not supported"})
                qs = parse_qs(parsed.query)
                record = state.find(qs.get("record_id", [None])[0],
                                    qs.get("portal_ref", [None])[0])
                if record:
                    return self._json(200, {"found": True, **record})
                return self._json(200, {"found": False})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" in ctype:
                params = json.loads(raw or "{}")
            else:
                params = {k: v[0] for k, v in parse_qs(raw).items()}

            if parsed.path == "/login":
                if params.get("username") == username and params.get("password") == password:
                    self.send_response(302)
                    self.send_header("Location", "/form")
                    self.send_header("Set-Cookie", "session=demo; HttpOnly")
                    self.end_headers()
                    return
                return self._json(401, {"error": "invalid credentials"})

            if parsed.path == "/api/submit":
                delay = float(params.get("delay_seconds", 0) or 0)
                if delay:
                    time.sleep(delay)  # 让浏览器侧先超时，制造“已点击、无回执”
                try:
                    amount = int(params.get("amount", 0))
                except (TypeError, ValueError):
                    amount = 0
                if amount > reject_amount_over:
                    return self._json(422, {"accepted": False,
                                            "error": "amount exceeds profile limit"})
                record_id = str(params.get("record_id") or uuid.uuid4().hex)
                portal_ref = "PTL-" + uuid.uuid4().hex[:10].upper()
                record = {
                    "record_id": record_id,
                    "portal_ref": portal_ref,
                    "form_id": params.get("form_id", ""),
                    "amount": amount,
                    "status": "accepted",
                    "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                  time.gmtime()),
                }
                state.add(record)
                return self._json(200, {"accepted": True, **record})
            self._json(404, {"error": "not found"})

    return Handler


def serve(port: int, state_file: str | None, queryable: bool,
          username: str, password: str, reject_amount_over: int) -> ThreadingHTTPServer:
    state = PortalState(state_file)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(state, username, password, queryable, reject_amount_over))
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Local demo form portal")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--mode", choices=["queryable", "non-queryable"],
                        default="queryable")
    parser.add_argument("--username", default="demo-user")
    parser.add_argument("--password", default="demo-pass")
    parser.add_argument("--reject-amount-over", type=int, default=10_000_000)
    args = parser.parse_args()
    server = serve(args.port, args.state_file, args.mode == "queryable",
                   args.username, args.password, args.reject_amount_over)
    print(json.dumps({"event": "portal_started", "port": args.port,
                      "mode": args.mode}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
