"""路径边界安全校验与派单前后改动范围门禁审计服务。"""

from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
from pathlib import Path

from codex_harness.model.entities import SENSITIVE_PATH_PATTERNS, ScopeAuditStatus
from codex_harness.schema.contracts import ScopeAuditResult

LOGGER = logging.getLogger(__name__)


def normalize_relative_path(workspace: Path, raw_path: str) -> str:
    """将输入路径规范化为工作区相对 POSIX 路径，拒绝越出工作区根目录的路径穿越。"""
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned:
        raise ValueError("Path cannot be empty")
    if cleaned.startswith("file:///"):
        cleaned = cleaned[8:]

    # 含通配符的规则单独做路径段检查
    if any(ch in cleaned for ch in ("*", "?", "[")):
        parts = [seg for seg in cleaned.split("/") if seg]
        if ".." in parts or cleaned.startswith("/") or (len(cleaned) >= 2 and cleaned[1] == ":"):
            raise ValueError(f"Unsafe glob pattern outside workspace: {raw_path!r}")
        return "/".join(parts)

    ws_resolved = workspace.resolve()
    candidate = Path(cleaned)
    resolved = candidate.resolve() if candidate.is_absolute() else (ws_resolved / candidate).resolve()
    try:
        rel = resolved.relative_to(ws_resolved)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace boundary: {raw_path!r}") from exc
    return rel.as_posix()


def _matches_rule(rel_path: str, rule: str) -> bool:
    """判断相对路径是否命中精确路径、目录前缀或 glob 规则。"""
    norm_path = rel_path.strip("/").lower()
    norm_rule = rule.strip("/").lower()
    if not norm_rule:
        return False
    if any(ch in norm_rule for ch in ("*", "?", "[")):
        return fnmatch.fnmatch(norm_path, norm_rule)
    return norm_path == norm_rule or norm_path.startswith(norm_rule + "/")


def _has_glob(path: str) -> bool:
    return any(ch in path for ch in ("*", "?", "["))


def _packet_rule_contains(limit: str, proposed: str) -> bool:
    """Accept only proposed rules whose full scope fits inside a packet rule."""
    if os.name == "nt":
        limit = limit.lower()
        proposed = proposed.lower()
    if limit == proposed:
        return True
    if limit.endswith("/"):
        return proposed.startswith(limit)
    if _has_glob(limit) and not proposed.endswith("/") and not _has_glob(proposed):
        return fnmatch.fnmatchcase(proposed, limit)
    return False


# NOTE: Codex 内部运行时同步文件与本地临时测试目录不属于代码变更，避免只读审查阶段产生误报
_IGNORED_RUNTIME_PREFIXES: tuple[str, ...] = (
    "skills/.system/",
    ".codex/",
    ".tmp/",
    ".pytest-tmp-",
    ".operations-test",
    "__pycache__/",
)


def _is_runtime_bookkeeping_path(rel_path: str) -> bool:
    """判断路径是否为 Codex 运行时自动释放的系统技能或临时缓存文件。"""
    norm = rel_path.strip("/").replace("\\", "/").lower()
    if any(norm.startswith(prefix) for prefix in _IGNORED_RUNTIME_PREFIXES):
        return True
    name = Path(norm).name
    return name.endswith((".sqlite-wal", ".sqlite-shm")) or name in {
        "logs_2.sqlite",
        "state_5.sqlite",
        "queue_1.sqlite",
        "goals_1.sqlite",
        "memories_1.sqlite",
        ".codex-global-state.sqlite",
        ".sqlite-maintenance.lock",
    }


def _is_sensitive_path(rel_path: str) -> bool:
    """检测路径是否命中敏感凭据或环境变量文件。"""
    name = Path(rel_path).name.lower()
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    return name in {pattern.lower() for pattern in SENSITIVE_PATH_PATTERNS}


class ScopeService:
    """负责白名单合法性校验、Git 脏文件采集以及派单后范围越界检测。"""

    def validate_allowed_files(self, workspace: Path, raw_paths: list[str]) -> list[str]:
        """校验并规范化允许改动的文件白名单，拒绝越界路径与敏感凭据文件。"""
        normalized: list[str] = []
        for raw in raw_paths:
            if not raw or not raw.strip():
                continue
            rel = normalize_relative_path(workspace, raw)
            if _is_sensitive_path(rel):
                raise ValueError(f"Sensitive file cannot be placed in allowed_files: {rel!r}")
            if rel not in normalized:
                normalized.append(rel)
        return normalized

    def validate_forbidden_paths(self, workspace: Path, raw_paths: list[str]) -> list[str]:
        """校验并规范化禁止触碰路径列表。"""
        normalized: list[str] = []
        for raw in raw_paths:
            if not raw or not raw.strip():
                continue
            rel = normalize_relative_path(workspace, raw)
            if rel not in normalized:
                normalized.append(rel)
        return normalized

    def constrain_packet_allowed_files(
        self,
        workspace: Path,
        packet_files: list[str],
        requested_files: list[str] | None,
    ) -> list[str]:
        """Use the packet's rules by default and permit only a narrowing of those rules."""
        def normalize_rules(raw_files: list[str]) -> list[str]:
            rules: list[str] = []
            for raw in raw_files:
                for rule in self.validate_allowed_files(workspace, [raw]):
                    if rule == ".":
                        raise ValueError("Task packet scope cannot include the entire workspace")
                    if not _has_glob(rule) and (raw.strip().endswith(("/", "\\")) or (workspace / rule).is_dir()):
                        rule += "/"
                    if rule not in rules:
                        rules.append(rule)
            return rules

        limits = normalize_rules(packet_files)
        if requested_files is None:
            return limits
        requested = normalize_rules(requested_files)
        outside = [rule for rule in requested if not any(_packet_rule_contains(limit, rule) for limit in limits)]
        if outside:
            raise ValueError(f"allowed_files exceeds the task packet change boundary: {', '.join(outside)}")
        return requested

    def list_git_dirty_files(self, workspace: Path) -> list[str]:
        """读取工作区当前 git status --porcelain 的相对路径列表（过滤运行时噪音，避免未跟踪大目录撑爆记录）。"""
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.debug("git status unavailable for %s: %s", workspace, exc)
            return []

        if proc.returncode != 0:
            return []

        files: list[str] = []
        for raw_line in proc.stdout.splitlines():
            if len(raw_line) < 4:
                continue
            path_part = raw_line[3:].strip()
            if " -> " in path_part:
                path_part = path_part.split(" -> ", 1)[1].strip()
            path_part = path_part.strip('"').replace("\\", "/")
            if not path_part or _is_runtime_bookkeeping_path(path_part):
                continue
            if path_part not in files:
                files.append(path_part)
        return files

    def audit_scope(
        self,
        workspace: Path,
        allowed_files: list[str],
        forbidden_paths: list[str],
        observed_files: list[str],
        read_only_expected: bool = False,
        packet_scope: bool = False,
    ) -> ScopeAuditResult:
        """比对实际观测到的改动文件与契约边界，输出结构化门禁审计结论。"""
        ws_resolved = workspace.resolve()
        norm_observed: list[str] = []
        for raw in observed_files:
            try:
                rel = normalize_relative_path(ws_resolved, raw)
            except ValueError:
                rel = raw.replace("\\", "/")
            if not rel or _is_runtime_bookkeeping_path(rel):
                continue
            if rel not in norm_observed:
                norm_observed.append(rel)

        forbidden_hits: list[str] = []
        out_of_scope: list[str] = []

        for path in norm_observed:
            if _is_sensitive_path(path) or any(_matches_rule(path, rule) for rule in forbidden_paths):
                forbidden_hits.append(path)
                continue
            if read_only_expected:
                out_of_scope.append(path)
                continue
            if packet_scope and not any(_packet_rule_contains(rule, path) for rule in allowed_files):
                out_of_scope.append(path)
            elif not packet_scope and allowed_files and not any(_matches_rule(path, rule) for rule in allowed_files):
                out_of_scope.append(path)

        if forbidden_hits or out_of_scope:
            status = ScopeAuditStatus.VIOLATION
            reasons: list[str] = []
            if read_only_expected and out_of_scope:
                reasons.append(f"只读审查阶段出现 {len(out_of_scope)} 个写改动")
            elif out_of_scope:
                reasons.append(f"发现 {len(out_of_scope)} 个白名单外改动")
            if forbidden_hits:
                reasons.append(f"触碰 {len(forbidden_hits)} 个禁止/敏感路径")
            summary = "范围门禁拦截：" + "，".join(reasons)
        elif not allowed_files and norm_observed and not read_only_expected:
            status = ScopeAuditStatus.WARNING
            summary = f"未声明 allowed_files 白名单，已观测到 {len(norm_observed)} 个改动文件（无敏感路径命中）。"
        else:
            status = ScopeAuditStatus.CLEAN
            if norm_observed:
                summary = f"全部 {len(norm_observed)} 个改动文件均在显式白名单边界内。"
            else:
                summary = "未产生越界改动（改动文件数 0）。"

        return ScopeAuditResult(
            status=status,
            allowed_files=allowed_files,
            forbidden_paths=forbidden_paths,
            observed_files=norm_observed,
            out_of_scope_files=out_of_scope,
            forbidden_hits=forbidden_hits,
            summary=summary,
        )
