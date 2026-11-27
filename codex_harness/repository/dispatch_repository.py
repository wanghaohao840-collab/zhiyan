"""Harness 显式派单记录的本地持久化仓储。"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

from codex_harness.schema.contracts import DispatchRoundRecord

LOGGER = logging.getLogger(__name__)
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,80}$")


class DispatchRepository:
    """管理 Harness 派单元数据与实时事件流文件，采用原子替换避免轮询读到半写入 JSON。"""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir.resolve()
        self._runs_dir = self._data_dir / "runs"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def runs_dir(self) -> Path:
        """返回派单存储目录路径。"""
        return self._runs_dir

    def _validate_run_id(self, run_id: str) -> str:
        """校验 run_id 格式，防止目录穿越。"""
        if not _RUN_ID_PATTERN.match(run_id):
            raise ValueError(f"Invalid run_id format: {run_id!r}")
        return run_id

    def record_path(self, run_id: str) -> Path:
        """返回指定派单的元数据 JSON 路径。"""
        safe_id = self._validate_run_id(run_id)
        return self._runs_dir / f"{safe_id}.json"

    def events_path(self, run_id: str) -> Path:
        """返回指定派单的事件流 JSONL 路径。"""
        safe_id = self._validate_run_id(run_id)
        return self._runs_dir / f"{safe_id}.events.jsonl"

    def save(self, record: DispatchRoundRecord) -> DispatchRoundRecord:
        """以临时文件原子替换方式写入派单记录。"""
        target = self.record_path(record.run_id)
        payload = record.model_dump_json(indent=2)
        with self._lock:
            tmp_path = target.with_suffix(".json.tmp")
            tmp_path.write_text(payload, encoding="utf-8")
            os.replace(tmp_path, target)
        return record

    def get(self, run_id: str) -> DispatchRoundRecord | None:
        """读取单条派单记录。"""
        try:
            target = self.record_path(run_id)
        except ValueError:
            return None
        with self._lock:
            if not target.exists():
                return None
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
                return DispatchRoundRecord.model_validate(raw)
            except (OSError, ValueError) as exc:
                LOGGER.warning("Failed to load dispatch record %s: %s", target, exc)
                return None

    def list_all(self) -> list[DispatchRoundRecord]:
        """按开始时间倒序列出全部派单记录。"""
        records: list[DispatchRoundRecord] = []
        with self._lock:
            for path in self._runs_dir.glob("*.json"):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    records.append(DispatchRoundRecord.model_validate(raw))
                except (OSError, ValueError) as exc:
                    LOGGER.warning("Skipping unreadable dispatch record %s: %s", path.name, exc)
        records.sort(key=lambda item: item.started_at, reverse=True)
        return records

    def list_by_session(self, session_id: str) -> list[DispatchRoundRecord]:
        """列出同一 Harness 会话下的全部派单轮次，按轮次升序排列。"""
        matched = [rec for rec in self.list_all() if rec.session_id == session_id]
        matched.sort(key=lambda item: (item.round_number, item.started_at))
        return matched

    def append_event_line(self, run_id: str, line: str) -> None:
        """向事件流 JSONL 文件追加一行原始事件。"""
        target = self.events_path(run_id)
        clean_line = line.rstrip("\r\n")
        if not clean_line:
            return
        with self._lock:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(clean_line + "\n")
