"""基于仓库方案文档、历史会话上下文与行业软件工程完成标准的派单规格自动推导服务。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from codex_harness.model.entities import SandboxMode, TaskType
from codex_harness.repository.dispatch_repository import DispatchRepository
from codex_harness.repository.packet_repository import PacketRepository
from codex_harness.repository.session_repository import SessionRepository
from codex_harness.schema.contracts import (
    PlanDocSummary,
    RoutingRequest,
    SpecInferenceRequest,
    SpecInferenceResult,
    TaskPacketInfo,
)
from codex_harness.service.routing_service import RoutingService
from codex_harness.service.scope_service import ScopeService

LOGGER = logging.getLogger(__name__)

_BACKTICK_TOKEN_RE = re.compile(r"`([^`\r\n]{2,140})`")
_CHECKBOX_UNCHECKED_RE = re.compile(r"^\s*-\s*\[\s\]\s+(.+)$", re.MULTILINE)
_CHECKBOX_ALL_RE = re.compile(r"^\s*-\s*\[[ xX]\]\s+", re.MULTILINE)
_PYTEST_CMD_RE = re.compile(r"(?:pytest\s+[^\r\n`]+|tests/[A-Za-z0-9_./-]+\.py)")
_FOLLOWUP_PREFIX_RE = re.compile(r"^(?:续轮派单\s*[:：]\s*)+")


@dataclass(frozen=True)
class DomainStandardProfile:
    """行业软件功能模块完成标准（Definition of Done）画像。"""

    key: str
    display_name: str
    keywords: tuple[str, ...]
    standard_summary: str
    acceptance_criteria: tuple[str, ...]
    candidate_code_globs: tuple[str, ...]
    candidate_test_globs: tuple[str, ...]
    flag_ownership: bool = False
    flag_concurrency: bool = False
    flag_cross_store: bool = False
    flag_complex: bool = False


# NOTE: 行业通用软件工程模块完成标准基线，结合仓库实际目录自动筛选真实存在路径与测试命令
INDUSTRY_DOMAIN_PROFILES: tuple[DomainStandardProfile, ...] = (
    DomainStandardProfile(
        key="api_auth_contract",
        display_name="API 契约、鉴权与文档预览/下载",
        keywords=(
            "api",
            "route",
            "路由",
            "接口",
            "preview",
            "原文预览",
            "预览",
            "下载",
            "download",
            "range",
            "auth",
            "鉴权",
            "归属",
            "越权",
            "404",
            "415",
            "422",
            "cookie",
            "schema",
            "document",
        ),
        standard_summary="REST/OpenAPI 契约与 OWASP IDOR 防越权标准",
        acceptance_criteria=(
            "【接口契约】请求与响应严格遵循 Pydantic Schema；非法参数确定性返回 422，不支持的流格式对归属用户返回 415，跨用户访问统一拒绝（401/403/404 防枚举）。",
            "【权限与隔离】服务端始终基于当前会话身份校验资源归属与删除栅栏，跨用户读/写/下载/删除均被拒绝，响应头禁止敏感数据缓存。",
            "【边界与编码】覆盖标准 UUID 与历史兼容 ID、Range 分段读取、字符集声明与空数据边界，且不破坏既有客户端契约。",
        ),
        candidate_code_globs=(
            "api/routes/documents.py",
            "api/routes/qa.py",
            "api/routes/search.py",
            "api/routes/reports.py",
            "api/schemas/",
            "app/document_library.py",
        ),
        candidate_test_globs=(
            "tests/api/test_document_routes.py",
            "tests/api/test_documents_routes.py",
            "tests/api/test_qa_routes.py",
            "tests/api/test_search_routes.py",
            "tests/api/test_reports_routes.py",
        ),
        flag_ownership=True,
    ),
    DomainStandardProfile(
        key="concurrency_worker",
        display_name="并发、事务、队列背压与租约 Worker",
        keywords=(
            "并发",
            "竞态",
            "race",
            "租约",
            "lease",
            "worker",
            "队列",
            "queue",
            "满载",
            "背压",
            "重试",
            "retry",
            "幂等",
            "事务",
            "transaction",
            "锁",
            "取消",
            "cancel",
            "拒写",
            "503",
        ),
        standard_summary="分布式租约 Fencing、CAS 原子提交与确定性并发回归标准",
        acceptance_criteria=(
            "【租约与原子提交】租约校验到最终持久化提交满足原子条件保护；旧 Worker 租约过期且新 Worker 接管提交后，旧 Worker 的迟到写入必须被拒绝，新提交结果保持可读。",
            "【背压与幂等】队列满载前原子预占位拒绝（如 503）且不污染历史状态；重试与取消具备幂等性，禁止嵌套重试造成重复副作用。",
            "【确定性并发验证】使用可控时钟、同步屏障或故障注入固定关键交错顺序，保留“修复前复现失败、修复后稳定通过”的自动化回归测试。",
        ),
        candidate_code_globs=(
            "app/qa_worker.py",
            "app/qa_service.py",
            "app/qa_job_repository.py",
            "app/graph_projection_worker.py",
            "app/summary_tasks.py",
        ),
        candidate_test_globs=(
            "tests/test_qa_worker.py",
            "tests/test_qa_service.py",
            "tests/test_qa_multi_summary_workflow.py",
            "tests/test_graph_projection_worker.py",
        ),
        flag_concurrency=True,
        flag_complex=True,
    ),
    DomainStandardProfile(
        key="storage_graph_consistency",
        display_name="存储、向量库、知识图谱与跨存储一致性",
        keywords=(
            "neo4j",
            "qdrant",
            "sqlite",
            "图谱",
            "graph",
            "建图",
            "删除",
            "fence",
            "栅栏",
            "迁移",
            "migration",
            "回滚",
            "rollback",
            "embedding",
            "向量",
            "跨存储",
            "一致性",
            "孤立",
        ),
        standard_summary="跨存储最终一致性、删除围栏与回滚安全标准",
        acceptance_criteria=(
            "【跨存储一致性】写入外部索引/图谱前先持久化清理与补偿责任；任一存储部分失败可追踪并重试收敛，不残留孤立节点或悬空引用。",
            "【删除围栏与隔离】文档删除与迟到建图/索引之间具备栅栏保护，用户命名空间与 document_id 双维隔离，严禁跨用户/跨文档串库。",
            "【兼容性与回退】不静默破坏既有 SQLite/Qdrant/Neo4j 持久化格式，变更具备可验证的迁移或冷备回退边界。",
        ),
        candidate_code_globs=(
            "hello_agents/memory/storage/neo4j_store.py",
            "hello_agents/memory/storage/qdrant_store.py",
            "hello_agents/memory/rag/qdrant_pipeline.py",
            "app/graph_service.py",
            "app/bootstrap.py",
        ),
        candidate_test_globs=(
            "tests/memory/test_neo4j_store.py",
            "tests/memory/test_qdrant_store.py",
            "tests/integration/test_neo4j_live.py",
            "tests/test_graph_service.py",
        ),
        flag_cross_store=True,
        flag_complex=True,
    ),
    DomainStandardProfile(
        key="rag_streaming_qa",
        display_name="RAG 检索、流式问答 SSE 与总结工作流",
        keywords=(
            "rag",
            "检索",
            "search",
            "问答",
            "qa",
            "stream",
            "流式",
            "sse",
            "总结",
            "summary",
            "引用",
            "citation",
            "langgraph",
            "脱敏",
            "grounding",
            "rerank",
            "finish_reason",
        ),
        standard_summary="RAG 可追溯引用、流式 SSE 生命周期与隐私脱敏标准",
        acceptance_criteria=(
            "【流式与资源释放】SSE / 流式接口实时感知断连并释放订阅与后台句柄，支持断流后从持久化消息恢复且无竞态丢尾帧。",
            "【引用与降级】单篇与多文档检索/总结严格受限于显式文档范围与上下文预算，来源引用（document_id / page_number / snippet）可准确追溯，部分文档失败时优雅降级。",
            "【日志与遥测脱敏】仅记录白名单状态与耗时指标，自动化测试注入哨兵串验证日志零泄漏原文、问题、回答、文件名与凭据。",
        ),
        candidate_code_globs=(
            "hello_agents/tools/builtin/rag_tool.py",
            "hello_agents/core/llm.py",
            "app/qa_answer_engine.py",
            "app/qa_multi_summary_workflow.py",
            "app/qa_observability.py",
            "app/document_search.py",
        ),
        candidate_test_globs=(
            "tests/test_qa_answer_engine.py",
            "tests/test_qa_multi_summary_workflow.py",
            "tests/tools/test_rag_tool_multi_document.py",
            "tests/test_document_search.py",
        ),
    ),
    DomainStandardProfile(
        key="frontend_ui_ux",
        display_name="前端 React/TypeScript 页面与交互体验",
        keywords=(
            "前端",
            "页面",
            "ui",
            "react",
            "web",
            "组件",
            "入口",
            "按钮",
            "布局",
            "视觉",
            "e2e",
            "playwright",
            "typescript",
            "切会话",
            "闪烁",
            "preview.ts",
        ),
        standard_summary="前端工程质量（类型安全、状态机防竞态、无 XSS 与响应式验收标准）",
        acceptance_criteria=(
            "【状态机与竞态防护】加载态、空态、错误态、成功态严格分离无闪烁；切会话或重复触发时丢弃过期异步响应，避免旧数据覆盖当前视图。",
            "【安全与可访问性】严禁使用 dangerouslySetInnerHTML，列表渲染提供稳定 key，桌面端与移动端核心交互无遮挡或截断。",
            "【构建与测试门禁】TypeScript 严格类型检查零报错、Vitest 单元/组件测试全部通过且 Vite 生产构建成功。",
        ),
        candidate_code_globs=(
            "web/src/pages/",
            "web/src/features/",
            "web/src/components/",
            "web/src/api/",
        ),
        candidate_test_globs=(),
    ),
    DomainStandardProfile(
        key="deploy_ops_backup",
        display_name="部署、容器编排与冷备恢复运维",
        keywords=(
            "部署",
            "deploy",
            "docker",
            "compose",
            "备份",
            "backup",
            "恢复",
            "restore",
            "脚本",
            "容器",
            "端口",
            "防火墙",
            "ops",
            "冷备",
        ),
        standard_summary="生产运维安全基线（隔离恢复演练、Fail-Closed 门禁与最小暴露面）",
        acceptance_criteria=(
            "【隔离演练】备份与恢复验证仅在新建隔离目录、卷和网络中运行，严禁覆盖或污染生产数据。",
            "【一致性校验】跨组件（SQLite / Qdrant / Neo4j）校验同批归档完整性；补偿失败时保持服务停机（fail-closed）并输出明确诊断。",
            "【网络安全】默认宿主机端口仅绑定 127.0.0.1，严禁提交或打印 .env、令牌及凭据。",
        ),
        candidate_code_globs=(
            "deploy/",
            "compose.yaml",
            "Dockerfile",
        ),
        candidate_test_globs=(
            "tests/deploy/",
        ),
        flag_cross_store=True,
    ),
    DomainStandardProfile(
        key="harness_plugin",
        display_name="Codex Harness / MCP 插件与监控面板",
        keywords=(
            "codex_harness",
            "harness",
            "mcp_server",
            "codex monitor",
            "派单",
            "插件",
            "marketplace",
            "ext-apps",
        ),
        standard_summary="MCP 协议规范、路径防穿越安全边界与单文件 UI 自包含标准",
        acceptance_criteria=(
            "【协议与沙箱】stdio JSON-RPC 2.0 二进制 UTF-8 通信无 CRLF 污染；路径白名单拒绝穿越与敏感文件，审查角色强制锁定 read-only。",
            "【监控与交互】前端单文件 MCP App 无外链依赖、明暗/自定义主题高对比度可读且无 dangerouslySetInnerHTML。",
            "【自动化验证】codex_harness 全部 pytest 单元测试与 UI TypeScript/Vite 构建通过。",
        ),
        candidate_code_globs=(
            "codex_harness/",
            "docs/agent-workflow/codex-delegation.md",
        ),
        candidate_test_globs=(
            "tests/test_codex_harness.py",
        ),
    ),
)

_BASELINE_CRITERIA: tuple[str, ...] = (
    "【缺陷复现与闭环】核心改动具备可观测的输入/输出与边界验收证据；修复类任务先用最小用例复现缺陷再验证通过。",
    "【范围与兼容性】严格限制修改在允许文件白名单内，不破坏既有公开接口契约、持久化格式与多用户/多文档隔离边界。",
    "【回归验证】受影响的单元测试及关联调用方回归测试全部通过，工作区无残留临时敏感文件。",
)


def strip_followup_prefix(title: str) -> str:
    """移除重复叠加的 '续轮派单：' 前缀并过滤误入的内部交接模板标题。"""
    cleaned = _FOLLOWUP_PREFIX_RE.sub("", title.strip()).strip()
    lower = cleaned.lower()
    if lower.startswith("inherited context (forked because") or lower.startswith(
        "antigravity -> codex explicit dispatch"
    ):
        return ""
    return cleaned


def _extract_doc_title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _extract_existing_paths_from_text(workspace: Path, text: str) -> list[str]:
    """从方案或会话文本中提取反引号或常见路径，并仅保留当前仓库真实存在的路径。"""
    ws = workspace.resolve()
    candidates: list[str] = []

    for token in _BACKTICK_TOKEN_RE.findall(text):
        cleaned = token.strip().split(":", 1)[0].replace("\\", "/")
        if "/" in cleaned or cleaned.endswith((".py", ".ts", ".tsx", ".yaml", ".yml", ".sh", ".ps1", ".md")):
            candidates.append(cleaned)

    # 也捕获未使用反引号的 tests/... 或 app/... 等明显相对路径
    for match in re.findall(
        r"\b(?:api|app|hello_agents|web|tests|deploy|codex_harness|docs)/[A-Za-z0-9_./-]+\.[A-Za-z0-9]+\b",
        text,
    ):
        candidates.append(match)

    resolved_rel: list[str] = []
    for raw in candidates:
        cleaned = raw.strip().lstrip("./")
        if not cleaned or ".." in cleaned.split("/") or cleaned.startswith(("/", "http")):
            continue
        target = (ws / cleaned).resolve()
        try:
            rel = target.relative_to(ws).as_posix()
        except ValueError:
            continue
        if target.exists() and rel not in resolved_rel:
            resolved_rel.append(rel)
    return resolved_rel


def _infer_sibling_tests(workspace: Path, code_paths: list[str]) -> list[str]:
    """根据代码路径在仓库 tests/ 中自动匹配已存在的对应单元测试文件。"""
    ws = workspace.resolve()
    tests_dir = ws / "tests"
    if not tests_dir.exists():
        return []

    found: list[str] = []
    for rel in code_paths:
        if rel.startswith("tests/"):
            if (ws / rel).exists() and rel not in found:
                found.append(rel)
            continue
        stem = Path(rel).stem
        if not stem or stem == "__init__":
            continue
        for candidate in (
            tests_dir / f"test_{stem}.py",
            tests_dir / "api" / f"test_{stem}.py",
            tests_dir / "api" / f"test_{stem}_routes.py",
            tests_dir / "memory" / f"test_{stem}.py",
            tests_dir / "tools" / f"test_{stem}.py",
            tests_dir / "deploy" / f"test_{stem}.py",
        ):
            if candidate.exists():
                rel_test = candidate.relative_to(ws).as_posix()
                if rel_test not in found:
                    found.append(rel_test)
    return found


class SpecInferenceService:
    """结合仓库方案、任务包、续轮上下文与行业软件工程完成标准，自动推导派单规格。"""

    def __init__(
        self,
        workspace: Path,
        session_repo: SessionRepository,
        dispatch_repo: DispatchRepository,
        packet_repo: PacketRepository,
        routing_service: RoutingService,
        scope_service: ScopeService,
    ) -> None:
        self._workspace = workspace.resolve()
        self._session_repo = session_repo
        self._dispatch_repo = dispatch_repo
        self._packet_repo = packet_repo
        self._routing_service = routing_service
        self._scope_service = scope_service

    def list_plan_docs(self, workspace: Path | None = None, limit: int = 30) -> list[PlanDocSummary]:
        """扫描仓库 docs/superpowers/plans/ 与 docs/agent-work/ 下的方案文档。"""
        ws = (workspace or self._workspace).resolve()
        docs: list[PlanDocSummary] = []
        scan_roots: list[tuple[Path, str]] = [
            (ws / "docs" / "superpowers" / "plans", "plan"),
            (ws / "docs" / "agent-work", "agent_work"),
        ]

        candidates: list[tuple[float, Path, str]] = []
        for root, category in scan_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                if path.is_file():
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    candidates.append((mtime, path, category))

        candidates.sort(key=lambda item: item[0], reverse=True)
        for mtime, path, category in candidates[:limit]:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(ws).as_posix()
            unchecked = len(_CHECKBOX_UNCHECKED_RE.findall(text))
            total = len(_CHECKBOX_ALL_RE.findall(text))
            docs.append(
                PlanDocSummary(
                    title=_extract_doc_title(text, path.stem),
                    relative_path=rel,
                    absolute_path=str(path.resolve()),
                    category="plan" if category == "plan" else "agent_work",
                    updated_at=datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                    unchecked_items=unchecked,
                    total_items=total,
                )
            )
        return docs

    def _find_matching_plan_doc(self, ws: Path, query_text: str) -> tuple[str, str] | None:
        """若用户未显式选方案文档，按标题/关键字自动匹配最相关的近期方案文档。"""
        lower_q = query_text.lower()
        if not lower_q.strip():
            return None

        best_score = 0
        best_result: tuple[str, str] | None = None
        for doc in self.list_plan_docs(ws, limit=20):
            score = 0
            # 强信号：如 P3、bge-m3、rag、summary、deployment、neo4j 等专题词命中文件名或标题
            doc_hay = f"{doc.relative_path} {doc.title}".lower()
            for token in re.findall(r"[a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,4}", lower_q):
                if len(token) >= 2 and token in doc_hay:
                    score += 3 if re.match(r"^p\d+$", token) else 1
            if score > best_score and score >= 3:
                try:
                    content = Path(doc.absolute_path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                best_score = score
                best_result = (doc.relative_path, content)
        return best_result

    def _build_python_pytest_cmd(self, ws: Path, test_targets: list[str]) -> str:
        """构造符合本仓库固定 venv 环境的 pytest 验收命令。"""
        win_venv = ws / "venv" / "Scripts" / "python.exe"
        py_cmd = ".\\venv\\Scripts\\python.exe" if win_venv.exists() else "python"
        basetemp = " --basetemp=.pytest-tmp-codex" if win_venv.exists() else ""
        if test_targets:
            joined = " ".join(test_targets[:6])
            return f"{py_cmd} -m pytest -q{basetemp} {joined}"
        return f"{py_cmd} -m pytest -q{basetemp}"

    def infer(self, req: SpecInferenceRequest, *, packet: TaskPacketInfo | None = None) -> SpecInferenceResult:
        """综合方案、任务包、续轮轨迹与行业完成标准，推导出完整可执行的派单规格。"""
        ws = Path(req.workspace).resolve() if req.workspace else self._workspace
        corpus_parts: list[str] = []
        matched_plan_docs: list[str] = []
        allowed_candidates: list[str] = []
        explicit_verify_cmds: list[str] = []
        unfinished_plan_items: list[str] = []
        session_context_summary = ""

        clean_title = strip_followup_prefix(req.title)
        if clean_title:
            corpus_parts.append(clean_title)
        if req.objective_hint.strip():
            corpus_parts.append(req.objective_hint.strip())

        # 1. 解析续轮会话历史（抓取已改动文件、运行过的命令、最后叙述与中断原因）
        session_id = req.resume_run_id or req.resume_codex_thread_id
        if session_id:
            detail = self._session_repo.get_session_detail(session_id)
            if detail is None:
                rec = self._dispatch_repo.get(session_id)
                if rec is not None:
                    corpus_parts.append(rec.title)
                    corpus_parts.append(rec.compiled_prompt[:1500])
                    allowed_candidates.extend(rec.allowed_files)
                    explicit_verify_cmds.extend(rec.verification_commands)
                    if not clean_title:
                        clean_title = strip_followup_prefix(rec.title)
            else:
                if not clean_title:
                    clean_title = strip_followup_prefix(detail.summary.title)
                corpus_parts.append(detail.summary.title)
                recent_narrations: list[str] = []
                last_error = ""
                for turn in detail.turns[-2:]:
                    if turn.prompt:
                        corpus_parts.append(turn.prompt[:1200])
                    if turn.error_message:
                        last_error = turn.error_message
                    for fpath in turn.changed_files:
                        try:
                            rel = Path(fpath).resolve().relative_to(ws).as_posix()
                            if (ws / rel).exists():
                                allowed_candidates.append(rel)
                        except (OSError, ValueError):
                            pass
                    for item in turn.timeline:
                        if item.kind == "narration" and item.text:
                            recent_narrations.append(item.text.strip())
                            corpus_parts.append(item.text[:600])
                        elif item.kind == "command" and item.command:
                            corpus_parts.append(item.command.command[:300])
                            for m in _PYTEST_CMD_RE.findall(item.command.command):
                                if m.startswith("tests/") and (ws / m).exists():
                                    allowed_candidates.append(m)
                if recent_narrations:
                    session_context_summary = f"接续先前会话进展（最近步骤：{recent_narrations[-1][:140]}）。"
                if last_error:
                    session_context_summary += " 需先核对中断前的半成品改动，补齐剩余实现并通过完整回归验收。"

        # 2. 解析显式绑定的任务包
        if req.packet_path:
            if packet is None:
                packet = PacketRepository(ws).find_packet(req.packet_path)
            if packet is None:
                raise ValueError(f"Task packet not found: {req.packet_path!r}")
            matched_plan_docs.append(packet.relative_path)
            corpus_parts.append(f"{packet.title}\n{packet.goal_excerpt}")
            explicit_verify_cmds.extend(packet.verification_commands)
            if not clean_title:
                clean_title = f"实现任务包 {packet.packet_id}: {packet.title}"

        # 3. 解析显式选择或自动命中的仓库方案文档
        plan_content = ""
        if req.plan_doc_path:
            candidate_doc = Path(req.plan_doc_path)
            if not candidate_doc.is_absolute():
                candidate_doc = ws / candidate_doc
            try:
                resolved_doc = candidate_doc.resolve()
                rel_doc = resolved_doc.relative_to(ws).as_posix()
                if resolved_doc.is_file():
                    plan_content = resolved_doc.read_text(encoding="utf-8", errors="replace")
                    matched_plan_docs.append(rel_doc)
                    if not clean_title:
                        clean_title = _extract_doc_title(plan_content, resolved_doc.stem)
            except (OSError, ValueError):
                LOGGER.warning("Ignored invalid plan_doc_path: %s", req.plan_doc_path)
        else:
            auto_plan = self._find_matching_plan_doc(ws, "\n".join(corpus_parts))
            if auto_plan is not None:
                rel_doc, plan_content = auto_plan
                matched_plan_docs.append(rel_doc)

        if plan_content:
            corpus_parts.append(plan_content[:4000])
            unfinished_plan_items = [
                item.strip() for item in _CHECKBOX_UNCHECKED_RE.findall(plan_content)[:4]
            ]
            allowed_candidates.extend(_extract_existing_paths_from_text(ws, plan_content))

        full_corpus = "\n".join(corpus_parts)
        lower_corpus = full_corpus.lower()
        allowed_candidates.extend(_extract_existing_paths_from_text(ws, full_corpus))

        # 4. 匹配行业软件功能模块完成标准画像
        scored_profiles: list[tuple[int, DomainStandardProfile]] = []
        for profile in INDUSTRY_DOMAIN_PROFILES:
            hits = sum(1 for kw in profile.keywords if kw in lower_corpus)
            # 风险勾选也加权到对应专业工程域
            if profile.flag_ownership and req.involves_ownership_check:
                hits += 2
            if profile.flag_concurrency and req.involves_concurrency_or_lease:
                hits += 2
            if profile.flag_cross_store and req.involves_cross_store_or_rollback:
                hits += 2
            if hits > 0:
                scored_profiles.append((hits, profile))

        scored_profiles.sort(key=lambda item: item[0], reverse=True)
        selected_profiles = [prof for _, prof in scored_profiles[:3]]

        # 5. 推导风险标记与任务类别
        sug_ownership = req.involves_ownership_check
        if sug_ownership is None:
            sug_ownership = any(p.flag_ownership for p in selected_profiles)
        sug_concurrency = req.involves_concurrency_or_lease
        if sug_concurrency is None:
            sug_concurrency = any(p.flag_concurrency for p in selected_profiles)
        sug_cross_store = req.involves_cross_store_or_rollback
        if sug_cross_store is None:
            sug_cross_store = any(p.flag_cross_store for p in selected_profiles)
        sug_complex = req.is_complex
        if sug_complex is None:
            sug_complex = len(selected_profiles) >= 2 or any(p.flag_complex for p in selected_profiles)

        task_intent = f"{clean_title}\n{req.objective_hint.strip()}".lower()
        is_review = re.search(r"(?:^|\n)\s*(?:review\b|audit\b|审查|审计|复核)", task_intent) or any(
            phrase in task_intent for phrase in ("独立审查", "独立复核", "只读检查", "independent review", "read-only review")
        )
        if req.task_type is not None:
            sug_task_type = req.task_type
        elif is_review:
            sug_task_type = TaskType.REVIEW_CONCURRENCY
        elif sug_concurrency or any(kw in task_intent for kw in ("修复", "缺陷", "竞态", "事务", "bug")):
            sug_task_type = TaskType.IMPLEMENT_TRANSACTION
        elif any(kw in task_intent for kw in ("整理日志", "更新进度", "文档整理", "readme")):
            sug_task_type = TaskType.CHORE
        else:
            sug_task_type = TaskType.IMPLEMENT_GENERAL

        # 6. 汇总行业完成标准验收条目
        criteria: list[str] = []
        standards_applied: list[str] = []
        matched_domains: list[str] = []
        for prof in selected_profiles:
            matched_domains.append(prof.display_name)
            standards_applied.append(prof.standard_summary)
            for item in prof.acceptance_criteria:
                if item not in criteria:
                    criteria.append(item)

        if not criteria:
            matched_domains.append("通用软件工程交付基线")
            standards_applied.append("最小复现、契约兼容与受控回归验收基线")
            criteria.extend(_BASELINE_CRITERIA)
        else:
            criteria.append(_BASELINE_CRITERIA[1])

        # 7. 组装真实存在的允许改动白名单（过滤敏感文件）
        for prof in selected_profiles:
            for rel_glob in (*prof.candidate_code_globs, *prof.candidate_test_globs):
                if (ws / rel_glob.rstrip("/")).exists() and rel_glob not in allowed_candidates:
                    allowed_candidates.append(rel_glob)

        sibling_tests = _infer_sibling_tests(ws, allowed_candidates)
        for test_path in sibling_tests:
            if test_path not in allowed_candidates:
                allowed_candidates.append(test_path)

        safe_allowed: list[str] = []
        for raw_path in allowed_candidates:
            try:
                validated = self._scope_service.validate_allowed_files(ws, [raw_path])
                for item in validated:
                    if item not in safe_allowed:
                        safe_allowed.append(item)
            except ValueError:
                continue
        safe_allowed = safe_allowed[:12]
        if packet is not None:
            safe_allowed = self._scope_service.constrain_packet_allowed_files(ws, packet.allowed_files, req.allowed_files)

        # 8. 自动生成对应模块的可执行验收命令
        verify_cmds: list[str] = []
        for cmd in explicit_verify_cmds:
            if cmd not in verify_cmds:
                verify_cmds.append(cmd)

        py_tests = [p for p in safe_allowed if p.startswith("tests/") and (p.endswith(".py") or p.endswith("/"))]
        if py_tests:
            pytest_cmd = self._build_python_pytest_cmd(ws, py_tests)
            if pytest_cmd not in verify_cmds:
                verify_cmds.append(pytest_cmd)

        has_frontend = any(p.startswith("web/") for p in safe_allowed) or any(
            p.key == "frontend_ui_ux" for p in selected_profiles
        )
        if has_frontend and (ws / "web" / "package.json").exists():
            for fe_cmd in ("npm --prefix web run typecheck", "npm --prefix web run test", "npm --prefix web run build"):
                if fe_cmd not in verify_cmds:
                    verify_cmds.append(fe_cmd)

        has_harness_ui = any(p.startswith("codex_harness/ui") for p in safe_allowed)
        if has_harness_ui and (ws / "codex_harness" / "ui" / "package.json").exists():
            verify_cmds.append("npm --prefix codex_harness/ui run build")

        if not verify_cmds:
            verify_cmds.append(self._build_python_pytest_cmd(ws, []))
        verify_cmds.append("git diff --check")

        # 9. 拼装结构化「目标与可验证完成条件」正文
        empty_packet_scope = packet is not None and not safe_allowed
        is_readonly_review = sug_task_type == TaskType.REVIEW_CONCURRENCY
        suggested_title = clean_title or (
            "执行独立只读并发与一致性审查" if is_readonly_review else "按方案执行工程实现与回归验收"
        )
        objective_lines: list[str] = []
        if req.objective_hint.strip():
            objective_lines.append(req.objective_hint.strip())
        else:
            plan_ref = f"依据方案 `{matched_plan_docs[0]}`，" if matched_plan_docs else ""
            if is_readonly_review:
                objective_lines.append(
                    f"{plan_ref}围绕「{suggested_title}」执行独立只读并发与一致性审查，逐项核查租约竞态、重复副作用、跨存储一致性与回退安全（只读分析，不修改代码）。"
                )
            elif empty_packet_scope:
                objective_lines.append(f"{plan_ref}围绕「{suggested_title}」完成只读核查并报告结果。")
            else:
                objective_lines.append(
                    f"{plan_ref}围绕「{suggested_title}」完成受控实现、边界加固与端到端可观测验收。"
                )
            if session_context_summary:
                objective_lines.append(session_context_summary)

        if unfinished_plan_items:
            objective_lines.append("")
            objective_lines.append("方案待完成要点：")
            for todo in unfinished_plan_items:
                objective_lines.append(f"- {todo}")

        objective_lines.append("")
        objective_lines.append("可验证完成条件（基于行业功能模块验收标准自动推导）：")
        for idx, crit in enumerate(criteria[:6], start=1):
            objective_lines.append(f"{idx}. {crit}")

        routing = self._routing_service.resolve(
            RoutingRequest(
                task_type=sug_task_type,
                involves_ownership_check=sug_ownership,
                involves_concurrency_or_lease=sug_concurrency,
                involves_cross_store_or_rollback=sug_cross_store,
                is_complex=sug_complex,
                model_override=req.model_override,
                effort_override=req.effort_override,
                sandbox_override=SandboxMode.READ_ONLY if empty_packet_scope else req.sandbox_override,
            )
        )

        forbidden_defaults = [
            ".env",
            "auth.json",
            "deploy-data/",
            "backups/",
        ]
        safe_forbidden = self._scope_service.validate_forbidden_paths(ws, forbidden_defaults)

        rationale = (
            f"已识别功能域「{' / '.join(matched_domains)}」，"
            f"套用行业基线（{'；'.join(standards_applied)}），"
            + (f"并结合方案文档 {', '.join(matched_plan_docs)} " if matched_plan_docs else "")
            + f"筛选出 {len(safe_allowed)} 个真实仓库路径与 {len(verify_cmds)} 条验收命令。"
        )

        return SpecInferenceResult(
            suggested_title=suggested_title,
            suggested_task_type=sug_task_type,
            suggested_involves_ownership_check=sug_ownership,
            suggested_involves_concurrency_or_lease=sug_concurrency,
            suggested_involves_cross_store_or_rollback=sug_cross_store,
            suggested_is_complex=sug_complex,
            objective="\n".join(objective_lines),
            acceptance_criteria=criteria[:6],
            allowed_files=safe_allowed,
            forbidden_paths=safe_forbidden,
            verification_commands=verify_cmds[:6],
            matched_domains=matched_domains,
            matched_plan_docs=matched_plan_docs,
            industry_standards_applied=standards_applied,
            rationale=rationale,
            routing_preview=routing,
        )
