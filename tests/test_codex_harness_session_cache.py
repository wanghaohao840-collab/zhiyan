"""回归测试：SessionRepository 缓存中的 running 状态在文件静默超时后应失效重算。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from codex_harness.repository.session_repository import SessionRepository


def test_session_repository_invalidates_stale_running_cache_after_idle_timeout(tmp_path: Path) -> None:
    """当未收到 task_complete 的 rollout 超过 120s 无写入时，缓存不得永久停留在 running。"""
    sessions_dir = tmp_path / "sessions" / "2026" / "11" / "28"
    sessions_dir.mkdir(parents=True)
    rollout = sessions_dir / "rollout-2026-11-28T21-50-00-thread-idle-001.jsonl"
    lines = [
        {
            "timestamp": "2026-11-28T13:50:00Z",
            "type": "session_meta",
            "payload": {
                "id": "thread-idle-001",
                "cwd": "D:\\python_self_agent",
                "originator": "Codex Desktop",
                "thread_source": "user",
                "timestamp": "2026-11-28T13:50:00Z",
            },
        },
        {
            "timestamp": "2026-11-28T13:50:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-1"},
        },
        {
            "timestamp": "2026-11-28T13:50:02Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-1",
                "item": {
                    "type": "UserMessage",
                    "content": [{"type": "text", "text": "验证静默超时缓存失效"}],
                },
            },
        },
    ]
    rollout.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in lines) + "\n", encoding="utf-8")

    repo = SessionRepository(session_roots=[tmp_path / "sessions"])

    # 刚写入时：摘要与详情均为 running 并写入缓存
    summaries_live = repo.list_sessions(limit=20)
    assert len(summaries_live) == 1
    assert summaries_live[0].status == "running"
    detail_live = repo.get_session_detail("thread-idle-001")
    assert detail_live is not None
    assert detail_live.summary.status == "running"

    # 模拟超过 2 分钟无新写入（通过回拨文件 mtime 模拟时间流逝，并伪造同 stat 命中缓存场景）
    old_ts = time.time() - 180.0
    os.utime(rollout, (old_ts, old_ts))
    stat = rollout.stat()
    key = str(rollout.resolve())
    repo._summary_cache[key] = (stat.st_mtime_ns, stat.st_size, summaries_live[0])
    repo._detail_cache[key] = (stat.st_mtime_ns, stat.st_size, detail_live)

    summaries_after = repo.list_sessions(limit=20)
    assert summaries_after[0].status == "completed"

    detail_after = repo.get_session_detail("thread-idle-001")
    assert detail_after is not None
    assert detail_after.summary.status == "completed"
    assert detail_after.turns[-1].status == "completed"
