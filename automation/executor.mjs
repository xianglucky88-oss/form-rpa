// Playwright 浏览器执行器：读取 Python 侧的交接请求，
// 把阶段检查点追加到 progress jsonl，最终原子写 result json。
//
// 用法：
//   node automation/executor.mjs --request spool/requests/att-xxx.json
//   node automation/executor.mjs --dry-run --request spool/requests/att-xxx.json
//
// 契约要点：
// - 账号口令只来自环境变量 RPA_USERNAME / RPA_PASSWORD，不写入任何文件或日志。
// - 点击提交之前的任何失败 => failed_pre_action / login_failed / page_changed，
//   这类失败确定没有外部动作，可以安全重试。
// - 点击之后拿不到可靠确认 => unknown，禁止调用方自动重试。
// - dry-run 不登录、不点击、不产生成功回执。
// - 结果文件用 tmp+rename 原子落盘，避免恢复方读到半截 JSON。

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";

function parseArgs(argv) {
  const args = { dryRun: argv.includes("--dry-run") };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--request") args.request = argv[i + 1];
    if (argv[i] === "--scenario") args.scenario = argv[i + 1];
    if (argv[i] === "--timeout-ms") args.timeoutMs = Number(argv[i + 1]);
  }
  return args;
}

const args = parseArgs(process.argv.slice(2));
if (!args.request) {
  throw new Error("--request <path> is required");
}
const request = JSON.parse(fs.readFileSync(args.request, "utf8"));
// 租约所有者（Python worker）必须原样回显；本进程 pid 仅作诊断
const executorId = request.executor_id || `node-${process.pid}`;
const runnerPid = process.pid;
const scenario = args.scenario || process.env.RPA_SCENARIO || "ok";
const timeoutMs = args.timeoutMs || Number(process.env.RPA_STEP_TIMEOUT_MS) || 15000;

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function checkpoint(phase, extra = {}) {
  const event = {
    attempt_id: request.attempt_id,
    executor_id: executorId,
    runner_pid: runnerPid,
    phase,
    event: extra.event || "phase",
    ts: nowIso(),
    ...Object.fromEntries(Object.entries(extra).filter(([, v]) => v !== undefined)),
  };
  fs.appendFileSync(request.progress_file, JSON.stringify(event) + "\n");
}

function writeResult(body) {
  const result = {
    attempt_id: request.attempt_id,
    executor_id: executorId,
    runner_pid: runnerPid,
    ts: nowIso(),
    dry_run: Boolean(request.dry_run || args.dryRun),
    ...body,
  };
  const tmp = `${request.result_file}.tmp-${process.pid}`;
  fs.writeFileSync(tmp, JSON.stringify(result, null, 2));
  fs.renameSync(tmp, request.result_file);
}

function evidenceDir() {
  const dir = request.evidence_dir || path.join(os.tmpdir(), request.attempt_id);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

async function screenshot(page, name) {
  try {
    const file = path.join(evidenceDir(), name);
    await page.screenshot({ path: file, timeout: 5000 });
    return file;
  } catch {
    return null;
  }
}

function failAndExit(outcome, phase, detail, evidence = []) {
  writeResult({ outcome, phase_reached: phase, detail, evidence });
  process.exit(0);
}

async function main() {
  const dryRun = Boolean(request.dry_run || args.dryRun);
  if (dryRun) {
    // 只读演练：打开门户登录页验证可达性，绝不登录或提交
    const { chromium } = await import("playwright");
    const browser = await chromium.launch({ headless: true });
    try {
      const page = await browser.newPage();
      await page.goto(request.portal_url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
      checkpoint("pre_click", { event: "dry_run_skip" });
      writeResult({
        outcome: "dry_run",
        phase_reached: "pre_click",
        detail: "dry-run: portal reachable, no login, no submit, no receipt",
        evidence: (await screenshot(page, "dry-run-login-page.png")) ? [] : [],
      });
    } catch (error) {
      writeResult({
        outcome: "failed_pre_action",
        phase_reached: "assigned",
        detail: `dry-run portal unreachable: ${error.name}`,
      });
    } finally {
      await browser.close();
    }
    return;
  }

  const username = process.env.RPA_USERNAME || "";
  const password = process.env.RPA_PASSWORD || "";
  if (!request.portal_url || !username || !password) {
    writeResult({
      outcome: "failed_pre_action",
      phase_reached: "assigned",
      detail: "missing portal url or credentials in environment",
    });
    return;
  }

  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const evidence = [];
  try {
    // ---- 阶段 1：登录（点击前）----
    checkpoint("login");
    await page.goto(request.portal_url, { waitUntil: "domcontentloaded", timeout: timeoutMs });
    await page.getByLabel("Username").fill(username);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: /sign in|login/i }).click();
    await page.waitForURL(/\/form/, { timeout: timeoutMs }).catch(() => {});
    if (!/\/form/.test(page.url())) {
      const shot = await screenshot(page, "login-failed.png");
      failAndExit("login_failed", "login",
        "portal did not grant session after sign in",
        shot ? [shot] : []);
    }

    // ---- 阶段 2：填表（仍在点击前）----
    checkpoint("pre_click");
    const submitButton = page.getByRole("button", { name: /submit|save/i });
    if (!(await submitButton.count())) {
      const shot = await screenshot(page, "page-changed.png");
      failAndExit("page_changed", "pre_click",
        "submit control missing; page structure changed",
        shot ? [shot] : []);
    }
    await page.getByLabel("Form ID").fill(request.form_id || "");
    const recordId = request.record_id
      || request.payload?.record_id
      || request.task_key.slice(0, 12);
    await page.getByLabel("Record ID").fill(recordId);
    await page.getByLabel("Amount").fill(String(request.payload?.amount ?? ""));

    // 注入演示场景：点击后让门户慢响应，浏览器侧先超时
    if (scenario === "portal_timeout") {
      await page.getByLabel(/响应延迟|delay/i).fill(String(Math.ceil(timeoutMs / 1000) + 5));
    }

    // ---- 阶段 3：点击提交。此后外部动作可能已经发生 ----
    checkpoint("submitted", { event: "submit_clicked" });
    const submitPromise = submitButton.click();
    let response;
    try {
      response = await page.waitForResponse(
        (r) => r.url().includes("/api/submit"), { timeout: timeoutMs });
    } catch {
      // 超时不等于失败：门户可能已受理。没有确认号，只能 unknown
      const shot = await screenshot(page, "after-submit-timeout.png");
      if (shot) evidence.push(shot);
      writeResult({
        outcome: "unknown",
        phase_reached: "submitted",
        detail: "no portal response after submit click; submission may have been accepted",
        evidence,
      });
      await submitPromise.catch(() => {});
      return;
    }
    await submitPromise.catch(() => {});
    checkpoint("awaiting_confirm");

    let body;
    try {
      body = await response.json();
    } catch {
      const shot = await screenshot(page, "unparseable-response.png");
      if (shot) evidence.push(shot);
      writeResult({
        outcome: "unknown",
        phase_reached: "awaiting_confirm",
        detail: "portal responded but body is not parseable confirmation",
        evidence,
      });
      return;
    }

    if (response.status() >= 400 || body.accepted === false) {
      // 门户明确拒绝：有外部动作但结论确定，属于业务失败，不重试
      const shot = await screenshot(page, "business-rejected.png");
      if (shot) evidence.push(shot);
      writeResult({
        outcome: "business_failed",
        phase_reached: "awaiting_confirm",
        detail: `portal rejected: ${body.error || response.status()}`,
        evidence,
      });
      return;
    }
    if (!body.portal_ref) {
      writeResult({
        outcome: "unknown",
        phase_reached: "awaiting_confirm",
        detail: "portal accepted without confirmation reference",
      });
      return;
    }

    // ---- 阶段 4：门户已确认，保存回执 ----
    checkpoint("receipt_saving", { portal_ref: body.portal_ref });
    if (scenario === "receipt_crash") {
      // 模拟回执保存前进程被杀：检查点里已留下 portal_ref 线索
      process.kill(process.pid, "SIGKILL");
    }
    const shot = await screenshot(page, "confirmation.png");
    if (shot) evidence.push(shot);
    const noteFile = path.join(evidenceDir(), "portal_confirmation.txt");
    fs.writeFileSync(noteFile,
      `portal_ref=${body.portal_ref}\nrecord_id=${body.record_id}\nconfirmed=true\n`);
    evidence.push(noteFile);

    writeResult({
      outcome: "succeeded",
      phase_reached: "done",
      portal_ref: body.portal_ref,
      detail: `portal accepted record ${body.record_id}`,
      evidence,
    });
  } catch (error) {
    const phase = fs.existsSync(request.progress_file)
      ? checkpointPhase() : "assigned";
    const shot = await screenshot(page, "error.png").catch(() => null);
    const detail = `${error.name}: ${error.message.split("\n")[0]}`;
    // 依据最后检查点决定：点击前异常可重试，点击后异常必须 unknown
    const postAction = ["submitted", "awaiting_confirm", "receipt_saving"].includes(phase);
    writeResult({
      outcome: postAction ? "unknown" : "failed_pre_action",
      phase_reached: phase,
      detail: postAction
        ? `error after submit click: ${detail}`
        : `error before submit click: ${detail}`,
      evidence: shot ? [shot] : [],
    });
  } finally {
    await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

function checkpointPhase() {
  try {
    const lines = fs.readFileSync(request.progress_file, "utf8")
      .split("\n").filter(Boolean);
    for (let i = lines.length - 1; i >= 0; i -= 1) {
      const evt = JSON.parse(lines[i]);
      if (evt.phase) return evt.phase;
    }
  } catch { /* ignore */ }
  return "assigned";
}

main().catch((error) => {
  const detail = `${error.name}: ${String(error.message || "").split("\n")[0]}`;
  try {
    writeResult({
      outcome: "unknown",
      phase_reached: checkpointPhase(),
      detail: `executor fatal: ${detail}`,
    });
  } catch {
    process.exit(2);
  }
});
