"""依据 AGENTS.md 实现的显式模型分工与风险升级路由服务。"""

from __future__ import annotations

from codex_harness.model.entities import HarnessRole, ReasoningEffort, SandboxMode, TaskType
from codex_harness.schema.contracts import RoutingDecision, RoutingRequest


class RoutingService:
    """将任务类型与风险标记映射为确定性的 Codex 模型、推理强度、沙箱与门禁清单。"""

    def resolve(self, req: RoutingRequest) -> RoutingDecision:
        """按 AGENTS.md 规则计算路由决策。"""
        gates: list[str] = ["先明确可验证的完成条件（操作顺序与必须成立的结果）"]
        fallback_note: str | None = None
        requires_astra_review = False

        if req.task_type == TaskType.REVIEW_CONCURRENCY:
            role = HarnessRole.ASTRA
            model = "gpt-6.2-astra"
            effort = ReasoningEffort.HIGH
            sandbox = SandboxMode.READ_ONLY
            requires_astra_review = True
            rationale = "涉及并发、租约过期、旧 Worker 拒写、跨存储一致性或回退安全，按 AGENTS.md 由 GPT-6.2 Astra High 独立只读审查。"
            gates.extend(
                [
                    "检查租约校验到最终写入之间的竞态、重试副作用与跨存储部分成功恢复责任",
                    "每项发现须给出代码位置、触发顺序、后果和可复现验证方法",
                    "审查阶段禁止直接修改仓库文件（强制 read-only 沙箱）",
                ]
            )
        elif req.task_type == TaskType.IMPLEMENT_TRANSACTION:
            role = HarnessRole.SOL
            model = "gpt-6.2-sol"
            effort = ReasoningEffort.HIGH
            sandbox = SandboxMode.WORKSPACE_WRITE
            rationale = "修复已定位的事务问题并补充回归测试，按 AGENTS.md 由 GPT-6.2 Sol High 负责实施。"
            gates.extend(
                [
                    "先用最小用例复现缺陷（保留修复前失败、修复后通过的证据）",
                    "并发/租约测试须使用可控时钟、同步屏障或故障注入固定顺序",
                ]
            )
            if req.involves_concurrency_or_lease or req.involves_cross_store_or_rollback:
                requires_astra_review = True
                gates.append("实现完成后须交 GPT-6.2 Astra High 独立复核最终交付差异")
        elif req.task_type == TaskType.CHORE:
            sandbox = SandboxMode.WORKSPACE_WRITE
            if req.luna_available:
                role = HarnessRole.LUNA
                model = "luna"
                effort = ReasoningEffort.HIGH
                rationale = "整理日志、更新进度或机械性修改，按 AGENTS.md 由 Luna High 承接。"
            else:
                role = HarnessRole.SOL
                model = "gpt-6.2-sol"
                effort = ReasoningEffort.MEDIUM
                fallback_note = "当前环境未提供 Luna，按 AGENTS.md 规则由 GPT-6.2 Sol Medium 承接。"
                rationale = "轻量整理/机械修改任务，按回退策略交由 GPT-6.2 Sol Medium 执行。"
            gates.append("严格限制改动边界，不得触碰核心业务或持久化逻辑")
        elif req.task_type == TaskType.PLAN_NEON:
            role = HarnessRole.NEON
            model = "gemini-4-neon-high"
            effort = ReasoningEffort.HIGH
            sandbox = SandboxMode.READ_ONLY
            rationale = "规划与任务包草案由独立只读规划模式生成，禁止写入与命令执行。"
            gates.append("仅在只读隔离资料目录规划，输出可执行任务包草案")
        else:
            # IMPLEMENT_GENERAL 或 CUSTOM
            role = HarnessRole.SOL
            model = "gpt-6.2-sol"
            escalate = (
                req.is_complex
                or req.involves_ownership_check
                or req.involves_concurrency_or_lease
                or req.involves_cross_store_or_rollback
            )
            effort = ReasoningEffort.HIGH if escalate else ReasoningEffort.MEDIUM
            sandbox = SandboxMode.WORKSPACE_WRITE
            reasons: list[str] = []
            if req.involves_ownership_check:
                reasons.append("涉及用户归属校验（须验证跨用户访问被拒绝）")
                gates.append("必须包含跨用户越权访问被拒绝的自动化测试")
            if req.involves_concurrency_or_lease or req.involves_cross_store_or_rollback:
                reasons.append("涉及并发竞争/跨存储写入/回退安全")
                requires_astra_review = True
                gates.append("实现后须由 GPT-6.2 Astra High 独立审查竞争与一致性路径")
            if req.is_complex:
                reasons.append("任务复杂度高需跨组件状态推理")
            if reasons:
                rationale = f"一般实现任务因「{'；'.join(reasons)}」升级至 GPT-6.2 Sol High。"
            else:
                rationale = "常规接口接入、迁移或功能开发，按 AGENTS.md 默认采用 GPT-6.2 Sol Medium。"

        if req.model_override:
            model = req.model_override.strip()
        if req.effort_override is not None:
            effort = req.effort_override
        if req.sandbox_override is not None:
            # NOTE: 审查角色强制保持只读，避免误配置导致审查代理直接改代码
            if req.task_type in (TaskType.REVIEW_CONCURRENCY, TaskType.PLAN_NEON):
                sandbox = SandboxMode.READ_ONLY
            else:
                sandbox = req.sandbox_override

        return RoutingDecision(
            task_type=req.task_type,
            role=role,
            model=model,
            effort=effort,
            sandbox=sandbox,
            requires_astra_review=requires_astra_review,
            fallback_note=fallback_note,
            rationale=rationale,
            required_gates=gates,
        )
