# Form RPA

线下业务表单的幂等排队、执行器领取、浏览器提交、回执回传与故障恢复闭环。

## 要解决的问题

表单按钮已经点下、但门户回执还没保存时执行器退出，系统必须回答运营人员：

- 这次点击**能不能重试**？
- 会不会**重复提交**？
- 证据在哪里，谁来决定下一步？

本系统把结论严格分成三类，且都以可查询的证据落库：

| 结论 | 触发条件 | 任务状态 | 后续动作 |
| --- | --- | --- | --- |
| 尚未产生外部动作 | 最后检查点在点击前（assigned/login/pre_click） | `failed_retryable` / `login_failed` / `page_changed` | 可安全自动重新领取 |
| 门户已确认成功 | 执行器带回确认号，或门户查询确认有记录 | `succeeded` | 归档，不再处理 |
| 可能已提交但无可靠证据 | 最后检查点在点击后（submitted/awaiting_confirm/receipt_saving） | `unknown` | **禁止自动重提**，只能门户查询或人工接管 |

门户不支持查询、或查询接口自身故障时，`unknown` 无法安全消除，任务进入
`awaiting_manual`，由运营人员依据每次尝试的证据决定。

## 架构与职责

```
JSON/CSV 输入
   │  校验（validate.py）
   ▼
SQLite 任务队列（receipts.py，单写者 + BEGIN IMMEDIATE）
   │  claim_next 原子领取，写租约 lease_expires_at
   ▼
交接目录 spool/（handoff.py，原子写）
   ├─ requests/<attempt>.json   不含口令的执行请求
   ├─ progress/<attempt>.jsonl  执行器逐阶段检查点（fsync）
   ├─ results/<attempt>.json    执行器原子写最终结果
   └─ evidence/<attempt>/       截图、门户确认文本
   │
   ├─ Node + Playwright（automation/executor.mjs，真实浏览器）
   └─ fake_executor.py（不依赖浏览器的失败/崩溃替身）
   │
   ▼
结果回传（record_result）→ 状态机落库 → confirmation.py 门户查询 → reports.py 运营视图
```

关键设计：

- **领取归属**：条件 `UPDATE ... WHERE state IN (可领取) AND lease 已失效`，
  以写锁仲裁；多个执行器（独立连接/独立进程）同时领取同一任务只有一个成功。
- **过期恢复**：租约过期的未结案尝试按最后检查点阶段推断，
  点击前=可重试，点击后=unknown；每次推断都留 `lease_events` 事件。
- **迟到结果防线**：结果回传校验 attempt 归属。任务已到终态 → `stale` 不覆盖；
  归属已被新尝试取代 → `late` 仅存证。两种情况下旧执行器都改不了任务结论。
- **幂等含义不变**：沿用基线的 `record_id + canonical payload` 哈希键，
  payload 原样保留；每次提交是 `submission_attempts` 里独立的一行，
  同一任务的多次尝试与回执分别可追踪。
- **提交顺序**：证据文件先 fsync 落盘，再提交数据库事务；
  连接使用 autocommit + 显式 `BEGIN IMMEDIATE/COMMIT`，无嵌套事务。
- **dry-run**：不登录、不点击、不接触提交接口，不产生成功回执，任务保持 `pending`。
- **敏感值**：口令只通过环境变量进执行器子进程，不写交接文件、不进数据库；
  所有日志经 `SecretRedactor` 脱敏（按登记值与通用 header/URL 模式兜底）。

## 目录

- `src/form_rpa/`
  - `models.py`：状态、阶段、结果枚举与数据类
  - `receipts.py`：SQLite 任务/尝试/确认/事件存储与状态机
  - `handoff.py`：跨语言交接文件协议（原子读写、结果校验）
  - `executor.py`：领取、子进程编排、心跳线程、结果结算与即时恢复
  - `fake_executor.py`：执行器替身，复现 8 种失败/崩溃场景
  - `confirmation.py`：支持/不支持查询两种门户的只读确认客户端
  - `demo_portal.py`：本地演示门户（stdlib HTTP 服务）
  - `reports.py`：运营证据视图与动作建议（retry/query/manual/done）
  - `security.py` / `applog.py`：脱敏与结构化日志
  - `clock.py`：测试/演练用可控时钟
- `automation/executor.mjs`：Playwright 浏览器执行器
- `scripts/demo_recovery.py`：连续恢复全流程演练
- `tests/`：30 个测试（含真实 Chromium 端到端）

## 快速开始

### 1. 入队

```bash
PYTHONPATH=src python3 -m form_rpa.cli \
  --database /tmp/rpa.sqlite3 enqueue --fixture fixtures/forms.json
```

### 2. 执行（先用不依赖浏览器的替身）

```bash
PYTHONPATH=src python3 -m form_rpa.cli \
  --database /tmp/rpa.sqlite3 worker --spool /tmp/rpa-spool \
  --runner fake --scenario ok
```

`--scenario` 可选：`ok login_exit pre_click_fail portal_timeout
receipt_crash business_reject login_failed page_changed`。
`--dry-run` 只做只读演练；`--task-key` 定向领取单个任务。

### 3. 恢复与查询确认

```bash
# 扫描过期租约（worker 进程早退时也会立即按检查点恢复，不必等定时扫描）
PYTHONPATH=src python3 -m form_rpa.cli --database /tmp/rpa.sqlite3 recover

# 支持查询的门户：unknown → succeeded（有记录）或 failed_retryable（查无）
PYTHONPATH=src python3 -m form_rpa.cli --database /tmp/rpa.sqlite3 \
  confirm --portal-url http://127.0.0.1:8765

# 不支持查询的门户：unknown → awaiting_manual
PYTHONPATH=src python3 -m form_rpa.cli --database /tmp/rpa.sqlite3 confirm --no-query
```

### 4. 运营证据视图

```bash
PYTHONPATH=src python3 -m form_rpa.cli --database /tmp/rpa.sqlite3 status --verbose
```

输出包含每个任务的全部尝试（阶段、结果、确认号、迟到标记、证据路径）、
门户查询记录和完整事件流，并给出 `continue/retry/query/manual/done` 建议。

### 5. 连续恢复演练（推荐先跑一遍）

```bash
python3 scripts/demo_recovery.py
```

依次演示：登录前退出→重试成功、点击前失败、门户接收后超时→unknown、
回执保存崩溃、查询确认三种结局、dry-run、8 执行器并发领取、租约过期恢复，
最后打印完整证据视图。

## 真实浏览器（Node + Playwright）

```bash
cd automation && npm install && npx playwright install chromium
# 口令只走环境变量
RPA_USERNAME=... RPA_PASSWORD=... \
PYTHONPATH=src python3 -m form_rpa.cli --database /tmp/rpa.sqlite3 worker \
  --spool /tmp/rpa-spool --runner node \
  --node-script automation/executor.mjs \
  --portal-url http://127.0.0.1:8765/
```

启动本地演示门户：

```bash
PYTHONPATH=src python3 -m form_rpa.demo_portal --port 8765 \
  --mode queryable          # 或 non-queryable 演示无法查证的门户
```

容器内无 root 安装系统库时，可把 arm64 依赖包解压到用户目录并设置
`LD_LIBRARY_PATH`（集成测试通过 `CHROME_LIB_DIR` 自动探测该目录）。

## 测试

```bash
python3 -m pytest
```

- Python 状态机/并发/恢复/脱敏测试不依赖浏览器。
- `tests/test_node_browser.py` 启动真实 Chromium 与本地门户，
  验证成功、业务拒绝、点击后超时、dry-run 与日志脱敏；
  缺少 node/浏览器时自动跳过。
