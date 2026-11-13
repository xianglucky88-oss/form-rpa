from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ---- 任务级状态 -------------------------------------------------------------

PENDING = "pending"                    # 已入队，等待领取
CLAIMED = "claimed"                    # 已被某个执行器领取，租约生效中
SUCCEEDED = "succeeded"                # 门户已确认成功（有确认号等可靠证据）
BUSINESS_FAILED = "business_failed"    # 门户明确业务拒绝（终态，不自动重试）
LOGIN_FAILED = "login_failed"          # 登录失败，未产生外部提交，可重试
PAGE_CHANGED = "page_changed"          # 页面结构变化，未产生外部提交，可重试
FAILED_RETRYABLE = "failed_retryable"  # 点击前的技术失败/崩溃恢复，可重试
UNKNOWN = "unknown"                    # 可能已提交但无可靠证据，禁止自动重试
AWAITING_MANUAL = "awaiting_manual"    # 无法安全消除未知，移交人工
REJECTED = "rejected"                  # 输入校验拒绝（沿用基线语义）

# 领取候选：只有确认“尚未产生外部动作”的状态才能重新排队
CLAIMABLE_STATES = (PENDING, FAILED_RETRYABLE, LOGIN_FAILED, PAGE_CHANGED)
# 终态：任何迟到结果都不得覆盖
TERMINAL_STATES = (SUCCEEDED, BUSINESS_FAILED, AWAITING_MANUAL, REJECTED)

# ---- 尝试级阶段（检查点） ----------------------------------------------------

PHASE_ASSIGNED = "assigned"            # 尝试已创建，执行器尚未开始
PHASE_LOGIN = "login"                  # 正在登录
PHASE_PRE_CLICK = "pre_click"          # 已登录、尚未点击提交
PHASE_SUBMITTED = "submitted"          # 已点击提交，等待门户响应
PHASE_AWAITING_CONFIRM = "awaiting_confirm"
PHASE_RECEIPT_SAVING = "receipt_saving"  # 门户已确认，正在落回执
PHASE_DONE = "done"                    # 执行器写完结果文件
PHASE_CRASHED = "crashed"              # 租约过期时仍未完成

# 点击前阶段：此阶段丢失进程可以确定没有外部动作
PRE_ACTION_PHASES = (PHASE_ASSIGNED, PHASE_LOGIN, PHASE_PRE_CLICK)
# 点击后阶段：此阶段丢失进程必须按“可能已提交”处理
POST_ACTION_PHASES = (
    PHASE_SUBMITTED,
    PHASE_AWAITING_CONFIRM,
    PHASE_RECEIPT_SAVING,
)

# ---- 尝试级结果 -------------------------------------------------------------

OUT_SUCCEEDED = "succeeded"
OUT_FAILED_PRE_ACTION = "failed_pre_action"
OUT_UNKNOWN = "unknown"
OUT_BUSINESS_FAILED = "business_failed"
OUT_LOGIN_FAILED = "login_failed"
OUT_PAGE_CHANGED = "page_changed"
OUT_DRY_RUN = "dry_run"

ALLOWED_TASK_STATES = {
    PENDING, CLAIMED, SUCCEEDED, BUSINESS_FAILED, LOGIN_FAILED,
    PAGE_CHANGED, FAILED_RETRYABLE, UNKNOWN, AWAITING_MANUAL, REJECTED,
}
ALLOWED_OUTCOMES = {
    OUT_SUCCEEDED, OUT_FAILED_PRE_ACTION, OUT_UNKNOWN, OUT_BUSINESS_FAILED,
    OUT_LOGIN_FAILED, OUT_PAGE_CHANGED, OUT_DRY_RUN,
}


@dataclass(frozen=True)
class FormRecord:
    record_id: str
    customer_code: str
    amount: int
    submitted_at: str
    fields: dict[str, str]


@dataclass(frozen=True)
class SubmissionTask:
    idempotency_key: str
    record_id: str
    payload: dict[str, str]
    state: str


@dataclass(frozen=True)
class Attempt:
    """一次提交尝试。同一个幂等任务可以有多次尝试，每次单独留证。"""

    attempt_id: str
    task_key: str
    attempt_no: int
    executor_id: str
    lease_expires_at: str
    phase: str = PHASE_ASSIGNED
    outcome: Optional[str] = None
    portal_ref: Optional[str] = None
    evidence_path: Optional[str] = None
    result_received_at: Optional[str] = None
    late: bool = False
    conflict_note: Optional[str] = None
    finalized_at: Optional[str] = None


@dataclass(frozen=True)
class AttemptResult:
    """执行器回传结果文件的解析结果（跨语言边界的契约）。"""

    attempt_id: str
    executor_id: str
    outcome: str
    phase_reached: str
    portal_ref: Optional[str] = None
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    dry_run: bool = False
    runner_pid: Optional[int] = None


@dataclass(frozen=True)
class RecoveryEvent:
    task_key: str
    attempt_id: str
    old_phase: str
    inferred_outcome: str
    new_task_state: str
    note: str


@dataclass(frozen=True)
class ConfirmationEvent:
    task_key: str
    portal_ref: Optional[str]
    confirmed: Optional[bool]      # None 表示门户不支持查询
    supports_query: bool
    note: str
