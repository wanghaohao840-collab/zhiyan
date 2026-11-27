"""仓库 docs/agent-workflow/task-packets 任务包只读扫描与解析仓储。"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from codex_harness.schema.contracts import PacketPlanGroup, TaskPacketInfo

LOGGER = logging.getLogger(__name__)

_PACKET_FILENAME_RE = re.compile(r"^\d+[-_].+\.md$", re.IGNORECASE)
_BACKTICK_PATH_RE = re.compile(r"`([^`\r\n]+)`")
_CODE_BLOCK_RE = re.compile(r"```(?:powershell|bash|sh|shell|pwsh)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
_VERDICT_RE = re.compile(r"^\s*-\s*(?:Verdict|Result)\s*:\s*`?([a-zA-Z0-9_-]+)`?", re.MULTILINE)


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """轻量解析 Markdown YAML frontmatter，避免引入额外第三方依赖。"""
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end_idx = normalized.find("\n---\n", 4)
    if end_idx == -1:
        return {}, normalized
    header = normalized[4:end_idx]
    body = normalized[end_idx + 5 :]

    result: dict[str, object] = {}
    current_list_key: str | None = None
    for raw_line in header.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- ") and current_list_key is not None:
            existing = result.setdefault(current_list_key, [])
            if isinstance(existing, list):
                existing.append(line[2:].strip().strip('"').strip("'"))
            continue
        if ":" not in line:
            current_list_key = None
            continue
        key, raw_val = line.split(":", 1)
        key = key.strip()
        val = raw_val.strip()
        current_list_key = None
        if not val:
            result[key] = []
            current_list_key = key
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            if not inner:
                result[key] = []
            else:
                result[key] = [
                    part.strip().strip('"').strip("'")
                    for part in inner.split(",")
                    if part.strip()
                ]
        elif val.lower() in ("true", "false"):
            result[key] = val.lower() == "true"
        else:
            result[key] = val.strip('"').strip("'")
    return result, body


def _extract_section(body: str, heading: str) -> str:
    """按二级标题提取 Markdown 章节正文。"""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$(.*?)(?=^##\s+|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(body)
    return match.group(1).strip() if match else ""


def _looks_like_repo_path(candidate: str) -> bool:
    """粗筛代码反引号中的仓库路径，过滤命令参数或普通符号名。"""
    cleaned = candidate.strip()
    if not cleaned or " " in cleaned or cleaned.startswith("-"):
        return False
    if "://" in cleaned or cleaned.startswith("<"):
        return False
    return "/" in cleaned or "\\" in cleaned or cleaned.endswith(
        (".py", ".ts", ".tsx", ".js", ".json", ".md", ".yaml", ".yml", ".toml", ".css", ".html", ".ps1")
    )


def _extract_allowed_and_forbidden(boundary_text: str) -> tuple[list[str], list[str]]:
    """从 Explicit change boundary 章节提取允许路径白名单与禁止变更摘要。"""
    allowed: list[str] = []
    forbidden: list[str] = []
    mode: str = "allowed"

    for raw_line in boundary_text.splitlines():
        stripped = raw_line.strip()
        lower = stripped.lower()
        if lower.startswith("### forbidden") or lower.startswith("- forbidden"):
            mode = "forbidden"
        elif lower.startswith("### allowed") or lower.startswith("- create:") or lower.startswith("- modify"):
            mode = "allowed"

        if mode == "forbidden":
            if stripped.startswith("- "):
                forbidden.append(stripped[2:].strip())
        else:
            for token in _BACKTICK_PATH_RE.findall(stripped):
                # 去掉 path:line 中的行号后缀
                base_token = token.split(":", 1)[0] if not re.match(r"^[A-Za-z]:\\", token) else token
                if _looks_like_repo_path(base_token) and base_token not in allowed:
                    allowed.append(base_token.replace("\\", "/"))

    return allowed, forbidden[:8]


class PacketRepository:
    """扫描并解析 docs/agent-workflow/task-packets 下的计划目录与编号任务包。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    @property
    def packets_root(self) -> Path:
        """返回任务包根目录。"""
        return self._workspace / "docs" / "agent-workflow" / "task-packets"

    def _read_verdict(self, doc_path: Path) -> str | None:
        """从 REVIEW.md 或 FINAL_INTEGRATION_REVIEW.md 提取结论字段。"""
        try:
            head = doc_path.read_text(encoding="utf-8", errors="replace")[:4000]
        except OSError:
            return None
        match = _VERDICT_RE.search(head)
        return match.group(1) if match else None

    def parse_packet_file(self, packet_path: Path, plan_name: str | None = None) -> TaskPacketInfo:
        """解析单个编号任务包 Markdown 文件。"""
        resolved = packet_path.resolve()
        text = resolved.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(text)

        derived_plan = plan_name or resolved.parent.name
        packet_id = str(meta.get("id") or resolved.stem)
        title = str(meta.get("title") or resolved.stem)
        status = str(meta.get("status") or "draft").lower()
        parallel_safe = bool(meta.get("parallel-safe", False))
        raw_deps = meta.get("depends-on")
        depends_on = [str(item) for item in raw_deps] if isinstance(raw_deps, list) else []
        base_commit = str(meta["base-commit"]) if meta.get("base-commit") else None
        owner = str(meta["owner"]) if meta.get("owner") else None

        goal_section = _extract_section(body, "Goal")
        goal_excerpt = " ".join(goal_section.splitlines()[:3]).strip()[:260]

        boundary_section = _extract_section(body, "Explicit change boundary")
        allowed_files, forbidden_notes = _extract_allowed_and_forbidden(boundary_section)

        verify_section = _extract_section(body, "Test and verification commands")
        commands: list[str] = []
        for block in _CODE_BLOCK_RE.findall(verify_section):
            for cmd_line in block.splitlines():
                cleaned = cmd_line.strip()
                if cleaned and not cleaned.startswith("#") and not cleaned.startswith("<"):
                    commands.append(cleaned)

        try:
            rel_path = resolved.relative_to(self._workspace).as_posix()
        except ValueError:
            rel_path = resolved.as_posix()

        return TaskPacketInfo(
            plan_name=derived_plan,
            packet_id=packet_id,
            title=title,
            status=status,
            parallel_safe=parallel_safe,
            depends_on=depends_on,
            base_commit=base_commit,
            owner=owner,
            relative_path=rel_path,
            absolute_path=str(resolved),
            goal_excerpt=goal_excerpt,
            allowed_files=allowed_files,
            forbidden_notes=forbidden_notes,
            verification_commands=commands[:6],
        )

    def list_plan_groups(self, plan_filter: str | None = None) -> list[PacketPlanGroup]:
        """按日期倒序返回所有任务包计划组，并计算依赖就绪状态。"""
        root = self.packets_root
        if not root.exists():
            return []

        groups: list[PacketPlanGroup] = []
        plan_dirs = sorted(
            [item for item in root.iterdir() if item.is_dir()],
            key=lambda item: item.name,
            reverse=True,
        )
        for plan_dir in plan_dirs:
            if plan_filter and plan_filter.lower() not in plan_dir.name.lower():
                continue

            review_file = plan_dir / "REVIEW.md"
            final_file = plan_dir / "FINAL_INTEGRATION_REVIEW.md"
            packet_files = sorted(
                [item for item in plan_dir.iterdir() if item.is_file() and _PACKET_FILENAME_RE.match(item.name)],
                key=lambda item: item.name,
            )

            packets: list[TaskPacketInfo] = []
            for packet_file in packet_files:
                try:
                    packets.append(self.parse_packet_file(packet_file, plan_name=plan_dir.name))
                except OSError as exc:
                    LOGGER.warning("Skipping unreadable packet %s: %s", packet_file, exc)

            status_by_id = {pkt.packet_id: pkt.status for pkt in packets}
            status_by_stem = {Path(pkt.relative_path).stem: pkt.status for pkt in packets}

            for pkt in packets:
                unmet: list[str] = []
                for dep in pkt.depends_on:
                    dep_status = status_by_id.get(dep) or status_by_stem.get(dep)
                    if dep_status != "done":
                        unmet.append(dep)
                pkt.unmet_dependencies = unmet
                pkt.dispatchable = pkt.status in ("ready", "blocked") and len(unmet) == 0

            done_count = sum(1 for pkt in packets if pkt.status == "done")
            groups.append(
                PacketPlanGroup(
                    plan_name=plan_dir.name,
                    directory_path=str(plan_dir.resolve()),
                    has_review=review_file.exists(),
                    review_path=str(review_file.resolve()) if review_file.exists() else None,
                    review_verdict=self._read_verdict(review_file) if review_file.exists() else None,
                    has_final_review=final_file.exists(),
                    final_review_path=str(final_file.resolve()) if final_file.exists() else None,
                    final_review_result=self._read_verdict(final_file) if final_file.exists() else None,
                    done_count=done_count,
                    total_count=len(packets),
                    packets=packets,
                )
            )
        return groups

    def find_packet(self, packet_path_or_id: str) -> TaskPacketInfo | None:
        """支持按相对路径、绝对路径或 packet_id 查找任务包。"""
        candidate = Path(packet_path_or_id)
        if not candidate.is_absolute():
            candidate = self._workspace / packet_path_or_id
        try:
            resolved = candidate.resolve()
            resolved.relative_to(self._workspace)
            if resolved.is_file():
                plan_groups = self.list_plan_groups(plan_filter=resolved.parent.name)
                for group in plan_groups:
                    for pkt in group.packets:
                        if Path(pkt.absolute_path).resolve() == resolved:
                            return pkt
                return self.parse_packet_file(resolved)
        except (OSError, ValueError):
            pass

        for group in self.list_plan_groups():
            for pkt in group.packets:
                if pkt.packet_id == packet_path_or_id or pkt.relative_path == packet_path_or_id:
                    return pkt
        return None
