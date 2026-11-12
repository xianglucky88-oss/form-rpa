# Form RPA

线下业务表单的文件监听、字段校验、幂等排队、RPA 回执和失败分类实现。

## 单一职责

- `inputs.py`：读取 CSV/JSON 表单输入。
- `validate.py`：字段校验和标准化。
- `queue.py`：根据幂等键创建或复用提交任务。
- `receipts.py`：保存成功、业务失败、登录失败和页面变化等结果。
- `pipeline.py`：编排输入到任务队列。
- `automation/submit_form.mjs`：Node.js + Playwright 的浏览器表单适配器，默认只接受 `--dry-run`，不内置账号或 token。

运行 Python 队列：

```bash
PYTHONPATH=src python3 -m form_rpa.cli --fixture fixtures/forms.json --database /tmp/form-rpa.sqlite3
```

运行 RPA dry-run：

```bash
node automation/submit_form.mjs --dry-run
```
