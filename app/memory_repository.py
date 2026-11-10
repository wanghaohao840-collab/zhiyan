from __future__ import annotations

import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from app.storage import read_json, write_json_atomic
from hello_agents.memory.base import Episode, MemoryItem


SUPPORTED_MEMORY_TYPES = {"working", "episodic", "semantic"}

class CorruptMemorySnapshotError(RuntimeError):
    """Raised when a memory snapshot cannot be safely restored."""


class MemorySnapshotRepository:
    def __init__(self, path: Path | str, user_id: str):
        self.path = Path(path)
        self.user_id = user_id

    def load_snapshot(self) -> dict[str, Any]:
        try:
            data = read_json(self.path, default={"user_id": self.user_id, "memories": []})
        except Exception as exc:
            raise CorruptMemorySnapshotError(
                f"Memory snapshot is corrupt and was not modified: {self.path}"
            ) from exc
        if not isinstance(data, dict):
            raise CorruptMemorySnapshotError(f"Invalid memory snapshot: {self.path}")

        # ── ownership validation (fail-closed) ──────────────────────
        persisted_user = data.get("user_id")
        if persisted_user is None:
            raise CorruptMemorySnapshotError(
                f"Memory snapshot is missing user_id — ownership unknown: {self.path}"
            )
        if not isinstance(persisted_user, str):
            raise CorruptMemorySnapshotError(
                f"Memory snapshot user_id is malformed ({type(persisted_user).__name__}): {self.path}"
            )
        if persisted_user != self.user_id:
            raise CorruptMemorySnapshotError(
                f"Memory snapshot belongs to user {persisted_user!r}, not {self.user_id!r}: {self.path}"
            )

        memories = data.get("memories", [])
        if not isinstance(memories, list):
            raise CorruptMemorySnapshotError(f"Invalid memory list: {self.path}")
        return {"user_id": self.user_id, "memories": memories}

    def save_from_manager(self, manager: Any) -> None:
        memories = []
        for memory_type, memory_module in getattr(manager, "memory_types", {}).items():
            if memory_type not in SUPPORTED_MEMORY_TYPES:
                continue

            if memory_type == "episodic":
                episodes = getattr(memory_module, "_episodes", {}) or {}
                episode_values = episodes.values() if isinstance(episodes, dict) else episodes
                for episode in episode_values:
                    context = dict(episode.context or {})
                    if context.get("user_id") != self.user_id:
                        continue
                    memories.append(
                        {
                            "id": episode.episode_id,
                            "content": episode.content,
                            "memory_type": "episodic",
                            "importance": float(context.get("importance", 0.5)),
                            "metadata": {
                                **context,
                                "session_id": episode.session_id,
                            },
                            "timestamp": episode.timestamp,
                        }
                    )
                continue

            container = getattr(memory_module, "memories", []) or []
            items = container.values() if isinstance(container, dict) else container
            for item in items:
                metadata = dict(item.metadata or {})
                if metadata.get("user_id") != self.user_id:
                    continue
                memories.append(
                    {
                        "id": item.id,
                        "content": item.content,
                        "memory_type": item.memory_type,
                        "importance": item.importance,
                        "metadata": metadata,
                        "timestamp": item.timestamp,
                    }
                )
        write_json_atomic(self.path, {"user_id": self.user_id, "memories": memories})

    def quarantine_and_reset(self) -> Path:
        """Quarantine the active snapshot and atomically replace with clean state.

        Failure-atomic: the active content is *copied* to backup first,
        then the clean replacement is written.  If the write fails the
        original is untouched.  Backup names include a UUID component so
        two quarantines in the same second never collide.
        """
        import shutil

        if not self.path.exists():
            raise FileNotFoundError(self.path)
        backup = self.path.with_name(
            f"{self.path.name}.corrupt-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        # 1. Stage backup durably before touching active.
        shutil.copy2(self.path, backup)
        try:
            # 2. Atomically write clean replacement.
            write_json_atomic(self.path, {"user_id": self.user_id, "memories": []})
        except Exception:
            # Roll back: remove the staged backup.  Active is intact.
            backup.unlink(missing_ok=True)
            raise
        return backup

    def restore(self, backup: Path | str) -> None:
        # Load the raw backup data without overwriting user_id.
        raw = read_json(Path(backup), default=None)
        if raw is None:
            raise FileNotFoundError(f"Backup not found: {backup}")
        if not isinstance(raw, dict):
            raise CorruptMemorySnapshotError(
                f"Backup has invalid root type: {backup}"
            )

        # ── strict ownership validation (Item 3) ──────────────────────
        # Backup user_id MUST be a non-empty string that strictly equals
        # self.user_id.  Missing, None, empty, and cross-user are all
        # rejected BEFORE any state mutation.
        backup_user = raw.get("user_id")
        if backup_user is None:
            raise CorruptMemorySnapshotError(
                f"Backup user_id is missing (None) — ownership unknown: {backup}"
            )
        if not isinstance(backup_user, str):
            raise CorruptMemorySnapshotError(
                f"Backup user_id must be str, got {type(backup_user).__name__}: {backup}"
            )
        if not backup_user.strip():
            raise CorruptMemorySnapshotError(
                f"Backup user_id is empty — ownership unknown: {backup}"
            )
        if backup_user != self.user_id:
            raise CorruptMemorySnapshotError(
                f"Backup belongs to user {backup_user!r}, not {self.user_id!r}"
            )

        # Validate schema before touching the active file.
        self.validate_schema(raw)
        write_json_atomic(self.path, raw)

    @staticmethod
    def validate_schema(data: Any) -> None:
        """Raise :exc:`CorruptMemorySnapshotError` if *data* does not
        conform to the required Memory snapshot schema.

        Callers should validate a candidate payload before writing it
        to the active snapshot path.
        """
        if not isinstance(data, dict):
            raise CorruptMemorySnapshotError(
                "Memory snapshot root must be a dict"
            )
        if "user_id" not in data:
            raise CorruptMemorySnapshotError(
                "Memory snapshot is missing user_id"
            )
        memories = data.get("memories")
        if not isinstance(memories, list):
            raise CorruptMemorySnapshotError(
                "Memory snapshot 'memories' field must be a list"
            )

    def restore_to_manager(self, manager: Any) -> None:
        """Restore snapshot data into *manager*, fail-closed.

        **Fail-closed contract (Item 4):**
        1. Clear all manager memory types FIRST.
        2. Build the full list of MemoryItem objects from the snapshot.
        3. Set them all at once (atomic from the caller's perspective).
        4. If ANY step fails, clear affected types and raise — never
           leave the manager in a half-restored state, and never report
           success when disk and memory are forked.
        """
        snapshot_data = self.load_snapshot()
        raw_items = snapshot_data.get("memories", [])

        # Pre-build all MemoryItem objects so we can validate before
        # touching any live state.
        staged: dict[str, list[Any]] = {}
        for item in raw_items:
            memory_type = item.get("memory_type")
            if memory_type not in SUPPORTED_MEMORY_TYPES:
                continue
            metadata = item.get("metadata") or {}
            if metadata.get("user_id") != self.user_id:
                continue
            staged.setdefault(memory_type, []).append(
                MemoryItem(
                    id=item.get("id"),
                    content=item.get("content", ""),
                    memory_type=memory_type,
                    importance=float(item.get("importance", 0.5)),
                    metadata=metadata,
                    timestamp=item.get("timestamp"),
                )
            )

        # ── Clear all first (fail-closed) ──────────────────────────
        memory_types = getattr(manager, "memory_types", {})
        failures: list[str] = []
        for memory_type, mem_mod in memory_types.items():
            embedder = getattr(mem_mod, "embedder", None)
            if embedder is None:
                embedder = getattr(mem_mod, "embedding_model", None)
            if getattr(embedder, "fingerprint", None):
                staged.setdefault(memory_type, [])
            if memory_type == "episodic" and hasattr(mem_mod, "_episodes"):
                try:
                    mem_mod._episodes.clear()
                    mem_mod.sessions.clear()
                except Exception as exc:
                    try:
                        mem_mod._episodes = {}
                        mem_mod.sessions = {}
                    except Exception:
                        failures.append(f"clear:{exc}")
                continue

            if hasattr(mem_mod, "memories"):
                try:
                    mem_mod.memories.clear()
                except Exception as exc:
                    try:
                        mem_mod.memories = {} if memory_type == "semantic" else []
                    except Exception:
                        failures.append(f"clear:{exc}")

        for memory_type, items in staged.items():
            memory_module = memory_types.get(memory_type)
            if memory_module is None:
                continue

            try:
                embedder = getattr(memory_module, "embedder", None)
                if embedder is None:
                    embedder = getattr(memory_module, "embedding_model", None)
                if getattr(embedder, "fingerprint", None):
                    # Snapshots contain original text. Rebuild only this user's
                    # vectors in the versioned memory collection after restart.
                    memory_module.vector_store.delete_by_filter(
                        memory_module.vector_collection,
                        {"user_id": self.user_id, "memory_type": memory_type},
                    )
                    for item in items:
                        memory_module.add(item)
                if memory_type == "episodic" and hasattr(memory_module, "_episodes"):
                    episodes = {
                        item.id: Episode(
                            episode_id=item.id,
                            session_id=item.metadata.get("session_id", "default"),
                            timestamp=item.timestamp,
                            content=item.content,
                            context={
                                **item.metadata,
                                "memory_type": "episodic",
                                "importance": item.importance,
                                "content": item.content,
                            },
                        )
                        for item in items
                    }
                    sessions: dict[str, list[str]] = {}
                    for episode in episodes.values():
                        sessions.setdefault(episode.session_id, []).append(episode.episode_id)
                    memory_module._episodes = episodes
                    memory_module.sessions = sessions
                elif hasattr(memory_module, "memories"):
                    memory_module.memories = (
                        {item.id: item for item in items}
                        if memory_type == "semantic"
                        else list(items)
                    )
            except Exception as exc:
                failures.append(f"{memory_type}: {exc}")
                try:
                    if memory_type == "episodic" and hasattr(memory_module, "_episodes"):
                        memory_module._episodes.clear()
                        memory_module.sessions.clear()
                    elif hasattr(memory_module, "memories"):
                        memory_module.memories.clear()
                except Exception:
                    pass

        if failures:
            # Fail-closed: clear everything so no disk-memory fork
            # can be misinterpreted as success.
            for memory_type, mem_mod in memory_types.items():
                embedder = getattr(mem_mod, "embedder", None)
                if embedder is None:
                    embedder = getattr(mem_mod, "embedding_model", None)
                if getattr(embedder, "fingerprint", None):
                    try:
                        mem_mod.vector_store.delete_by_filter(
                            mem_mod.vector_collection,
                            {"user_id": self.user_id, "memory_type": memory_type},
                        )
                    except Exception:
                        pass
                if memory_type == "episodic" and hasattr(mem_mod, "_episodes"):
                    try:
                        mem_mod._episodes.clear()
                        mem_mod.sessions.clear()
                    except Exception:
                        pass
                elif hasattr(mem_mod, "memories"):
                    try:
                        mem_mod.memories.clear()
                    except Exception:
                        pass
            raise RuntimeError(
                f"restore_to_manager partially failed (fail-closed): "
                f"{'; '.join(failures)}.  All memory types cleared."
            )
