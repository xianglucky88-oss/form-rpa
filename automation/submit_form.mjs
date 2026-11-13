// 兼容入口：基线版本的独立 dry-run 脚本。
// 完整的领取/检查点/回执闭环请使用 automation/executor.mjs，
// 由 Python 侧 form_rpa.cli worker 通过交接文件驱动。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const keys = ["RPA_PORTAL_URL", "RPA_USERNAME", "RPA_PASSWORD", "RPA_FORM_ID"];
const dryRun = process.argv.includes("--dry-run");
const config = Object.fromEntries(keys.map((key) => [key, process.env[key] ?? ""]));

if (dryRun) {
  // dry-run 仍然不登录、不提交、不产生成功回执
  console.log(JSON.stringify({
    mode: "dry-run",
    configured: Boolean(config.RPA_PORTAL_URL),
    formIdPresent: Boolean(config.RPA_FORM_ID),
    notice: "use executor.mjs with Python handoff for full lifecycle",
  }));
} else {
  // 构造一份最小交接请求并委托给 executor.mjs，避免两套浏览器逻辑漂移
  const here = path.dirname(fileURLToPath(import.meta.url));
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "form-rpa-legacy-"));
  const request = {
    attempt_id: `legacy-${process.pid}`,
    task_key: `legacy-${process.pid}`,
    attempt_no: 1,
    portal_url: config.RPA_PORTAL_URL,
    form_id: config.RPA_FORM_ID,
    payload: {},
    dry_run: false,
    heartbeat_seconds: 5,
    progress_file: path.join(dir, "progress.jsonl"),
    result_file: path.join(dir, "result.json"),
    evidence_dir: path.join(dir, "evidence"),
  };
  const missing = keys.filter((key) => !config[key]);
  if (missing.length) throw new Error(`missing RPA configuration: ${missing.join(", ")}`);
  const requestPath = path.join(dir, "request.json");
  fs.writeFileSync(requestPath, JSON.stringify(request));
  const { spawnSync } = await import("node:child_process");
  const run = spawnSync(process.execPath,
    [path.join(here, "executor.mjs"), "--request", requestPath],
    { stdio: "inherit", env: process.env });
  if (run.status === 0 && fs.existsSync(request.result_file)) {
    const result = JSON.parse(fs.readFileSync(request.result_file, "utf8"));
    console.log(JSON.stringify(result));
  }
  process.exit(run.status ?? 1);
}
