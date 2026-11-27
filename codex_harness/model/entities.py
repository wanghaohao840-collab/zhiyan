"""Codex Harness 核心领域枚举与常量定义。"""

from __future__ import annotations

from enum import StrEnum


class HarnessRole(StrEnum):
    """对应 AGENTS.md 定义的协同角色。"""

    SOL = "sol"
    ASTRA = "astra"
    LUNA = "luna"
    NEON = "neon"
    CUSTOM = "custom"


class TaskType(StrEnum):
    """用于自动匹配模型与风险门禁的任务类别。"""

    IMPLEMENT_TRANSACTION = "implement_transaction"
    IMPLEMENT_GENERAL = "implement_general"
    REVIEW_CONCURRENCY = "review_concurrency"
    CHORE = "chore"
    PLAN_NEON = "plan_neon"
    CUSTOM = "custom"


class ReasoningEffort(StrEnum):
    """Codex 推理强度档位。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    ULTRA = "ultra"


class SandboxMode(StrEnum):
    """Codex CLI 执行沙箱策略。"""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


class DispatchStatus(StrEnum):
    """显式派单运行状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"


class ScopeAuditStatus(StrEnum):
    """派单后改动范围门禁审计结论。"""

    UNCHECKED = "unchecked"
    CLEAN = "clean"
    VIOLATION = "violation"
    WARNING = "warning"


# NOTE: 默认受保护的敏感文件名，任何自动派单白名单都不得直接放行
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    ".env",
    "auth.json",
    "auth.json.bak",
    ".cockpit_codex_auth.json",
    ".cockpit_codex_auth.json.bak",
)
