"""User-scoped corruption recovery for History and Memory snapshots.

Provides explicit inspect / quarantine / reset / restore operations under
the per-user coordination lock, using opaque backup identifiers that never
expose filesystem paths to callers.

Invariants
----------
* Corruption blocks writes until explicit recovery (fail-closed).
* Backup remains available after quarantine.
* Restore validates type/schema before atomic replacement.
* One user cannot enumerate or restore another user's backup.
* UI-visible error messages never contain absolute filesystem paths.
* Memory recovery keeps the live MemoryManager in sync with the file.
"""

from __future__ import annotations

import logging
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.history import EMPTY_HISTORY, CorruptHistoryError, HistoryRepository
from app.memory_repository import CorruptMemorySnapshotError, MemorySnapshotRepository
from app.storage import read_json, write_json_atomic

logger = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    """Outcome of a single recovery operation.

    Never exposes filesystem paths — *backup_id* is an opaque filename.
    """

    success: bool
    action: str       # 'check', 'quarantine', 'restore', 'list'
    target: str       # 'history', 'memory'
    message: str
    backup_id: str | None = None


class RecoveryService:
    """User-scoped corruption recovery for History and Memory snapshots.

    Every mutation acquires the per-user coordination lock.  Backup
    identifiers are opaque filenames resolved against the user's private
    backup directory — callers never supply or receive absolute filesystem
    paths.

    The live MemoryTool's MemoryManager is kept in sync during quarantine
    and restore so that a later manager save cannot resurrect quarantined
    state or overwrite restored state.

    Usage::

        svc = RecoveryService(coordinator, history_repo, memory_repo,
                              backup_dir, memory_manager=manager)

        # Inspect
        result = svc.check_history()
        if not result.success:
            # Quarantine corrupt data
            q = svc.quarantine_history()
            # Later, restore
            svc.restore_history(q.backup_id)
    """

    def __init__(
        self,
        coordinator: Any,  # UserMutationCoordinator
        history_repo: HistoryRepository,
        memory_repo: MemorySnapshotRepository,
        backup_dir: Path | str,
        *,
        memory_manager: Any = None,  # MemoryManager from MemoryTool
    ) -> None:
        self._coordinator = coordinator
        self._history = history_repo
        self._memory = memory_repo
        self._backup_dir = Path(backup_dir)
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        self._memory_manager = memory_manager

    @property
    def user_id(self) -> str:
        return self._coordinator.user_id

    # ── inspection ────────────────────────────────────────────────────

    def check_history(self) -> RecoveryResult:
        """Check whether the active History snapshot is readable.

        The returned message never contains a filesystem path — diagnostic
        details are logged internally.
        """
        try:
            self._history.load()
            return RecoveryResult(True, "check", "history",
                                  "History is readable")
        except CorruptHistoryError as exc:
            logger.warning("History corruption detected for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(False, "check", "history",
                                  "History is corrupt and cannot be read. "
                                  "Use Quarantine to recover.")

    def check_memory(self) -> RecoveryResult:
        """Check whether the active Memory snapshot is readable.

        The returned message never contains a filesystem path — diagnostic
        details are logged internally.
        """
        try:
            self._memory.load_snapshot()
            return RecoveryResult(True, "check", "memory",
                                  "Memory snapshot is readable")
        except CorruptMemorySnapshotError as exc:
            logger.warning("Memory corruption detected for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(False, "check", "memory",
                                  "Memory snapshot is corrupt and cannot be read. "
                                  "Use Quarantine to recover.")

    # ── quarantine ────────────────────────────────────────────────────

    def quarantine_history(self) -> RecoveryResult:
        """Quarantine a corrupt (or suspicious) History snapshot and
        replace the active file with a clean empty History.

        The corrupt snapshot is preserved in the user's private backup
        directory.  Returns an opaque *backup_id* that can be passed to
        :meth:`restore_history`.

        **Failure compensation (Item 2):** if ``_store_backup()`` fails
        after the clean reset, the backup is restored to the active
        position so the caller's data is never silently lost.
        """
        with self._coordinator.lock:
            try:
                backup_path = self._history.quarantine_and_reset()
            except FileNotFoundError:
                self._history.save(deepcopy(EMPTY_HISTORY))
                return RecoveryResult(
                    True, "quarantine", "history",
                    "No history file existed; created clean history",
                )

            try:
                backup_id = self._store_backup(backup_path)
            except Exception:
                # ── Item 2+3 compensation ────────────────────────
                # _store_backup failed — the active file is already
                # clean but the original data still exists at
                # backup_path.  Restore active from backup, then
                # clean up the staged file.
                logger.warning(
                    "History _store_backup failed for %s — rolling back",
                    self.user_id, exc_info=True,
                )
                rollback_ok = False
                try:
                    self._history.restore(backup_path)
                    rollback_ok = True
                except Exception:
                    logger.error(
                        "History rollback also failed for %s — "
                        "preserving staged backup as emergency",
                        self.user_id, exc_info=True,
                    )
                if rollback_ok:
                    # Active was restored — staged backup is redundant.
                    try:
                        backup_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    # ── Item 3: double failure ──────────────────
                    # The staged backup is the LAST copy.  Move it
                    # into the backup directory so list_*_backups()
                    # can find it — never delete the last copy.
                    self._emergency_preserve(
                        backup_path, "history-store-fail"
                    )
                raise

            logger.info("History quarantined for %s, backup=%s",
                        self.user_id, backup_id)
            return RecoveryResult(
                True, "quarantine", "history",
                f"History quarantined and reset. Backup ID: {backup_id}",
                backup_id=backup_id,
            )

    def quarantine_memory(self) -> RecoveryResult:
        """Quarantine a corrupt Memory snapshot and replace the active
        file with a clean empty snapshot.  The live MemoryManager is also
        cleared so that a later manager save cannot resurrect the
        quarantined state.

        Returns an opaque *backup_id* for :meth:`restore_memory`.

        **Compensation guarantees:**

        * **Item 1:** if ``clear_all()`` throws after the disk was
          already reset, the disk is restored from the staged backup so
          no forked state (clean disk + old memory) exists.
        * **Item 2:** if ``_store_backup()`` fails, both the snapshot
          file and the live MemoryManager are rolled back.
        * **Item 3:** if ``_store_backup`` **and** the active restore
          both fail, the staged backup is emergency-preserved in the
          user backup directory — the last copy is never deleted.
        """
        with self._coordinator.lock:
            try:
                backup_path = self._memory.quarantine_and_reset()
            except FileNotFoundError:
                write_json_atomic(
                    self._memory.path,
                    {"user_id": self.user_id, "memories": []},
                )
                # Clear in-memory state as well.
                if self._memory_manager is not None:
                    self._memory_manager.clear_all()
                return RecoveryResult(
                    True, "quarantine", "memory",
                    "No memory snapshot existed; created clean snapshot",
                )

            # ── Clear live manager ───────────────────────────────
            # quarantine_and_reset already reset the disk to empty.
            # Now sync the in-memory manager.
            if self._memory_manager is not None:
                try:
                    self._memory_manager.clear_all()
                except Exception:
                    logger.warning(
                        "Memory clear_all failed for %s — "
                        "force-clearing memories directly",
                        self.user_id, exc_info=True,
                    )
                    # Force-clear each memory type directly.
                    force_ok = True
                    for mem_mod in self._memory_manager.memory_types.values():
                        if hasattr(mem_mod, "memories"):
                            try:
                                mem_mod.memories.clear()
                            except Exception:
                                force_ok = False
                    if not force_ok:
                        # ── Item 1: double-clear failure ─────────────
                        # Both clear_all() AND at least one
                        # mem_mod.memories.clear() failed.
                        # Restore disk from staged backup AND try to
                        # restore the live manager.  Never return
                        # quarantine success on this path.
                        logger.warning(
                            "Double-clear failure for %s — "
                            "rolling back disk and manager",
                            self.user_id,
                        )
                        disk_restored = False
                        try:
                            self._memory.restore(backup_path)
                            disk_restored = True
                        except Exception:
                            logger.error(
                                "Disk restore also failed for %s — "
                                "disk-memory fork possible; "
                                "staged backup at %s",
                                self.user_id, backup_path,
                                exc_info=True,
                            )

                        manager_restored = False
                        if disk_restored and self._memory_manager is not None:
                            try:
                                self._memory.restore_to_manager(
                                    self._memory_manager
                                )
                                manager_restored = True
                            except Exception:
                                logger.error(
                                    "Manager restore failed "
                                    "for %s after double-clear",
                                    self.user_id, exc_info=True,
                                )

                        if disk_restored and manager_restored:
                            # Rollback succeeded — disk and manager
                            # both have the original data.  Store the
                            # backup (it's the only enumerable copy)
                            # and return FAILURE because quarantine
                            # did not happen.
                            try:
                                backup_id = self._store_backup(
                                    backup_path
                                )
                            except Exception:
                                self._emergency_preserve(
                                    backup_path,
                                    "memory-double-clear",
                                )
                                raise
                            return RecoveryResult(
                                False, "quarantine", "memory",
                                "Quarantine failed: clear_all and "
                                "force-clear both failed.  Rolled "
                                "back to original state.  Backup "
                                f"ID: {backup_id}",
                                backup_id=backup_id,
                            )
                        else:
                            # Either disk or manager could not be
                            # restored — fail-closed with emergency
                            # preserve (Item 3).
                            self._emergency_preserve(
                                backup_path,
                                "memory-double-clear",
                            )
                            raise RuntimeError(
                                "quarantine_memory failed after "
                                "double-clear failure: "
                                f"disk_restored={disk_restored}, "
                                f"manager_restored={manager_restored}. "
                                "Staged backup emergency-preserved."
                            )

            # ── Persist backup ───────────────────────────────────
            try:
                backup_id = self._store_backup(backup_path)
            except Exception:
                # ── Item 2+3 compensation ────────────────────────
                # _store_backup failed.
                logger.warning(
                    "Memory _store_backup failed for %s — rolling back",
                    self.user_id, exc_info=True,
                )
                rollback_ok = False
                # 1. Restore the snapshot file from the staged backup.
                try:
                    self._memory.restore(backup_path)
                    rollback_ok = True
                except Exception:
                    logger.error(
                        "Memory file rollback also failed for %s",
                        self.user_id, exc_info=True,
                    )
                # 2. Restore the live MemoryManager.
                if self._memory_manager is not None:
                    try:
                        self._memory.restore_to_manager(
                            self._memory_manager
                        )
                    except Exception:
                        logger.error(
                            "Memory manager rollback failed for %s",
                            self.user_id, exc_info=True,
                        )
                if rollback_ok:
                    # Active was restored — staged backup is redundant.
                    try:
                        backup_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                else:
                    # ── Item 3: double failure ──────────────────
                    # The staged backup is the LAST copy.
                    self._emergency_preserve(
                        backup_path, "memory-store-fail"
                    )
                raise

            logger.info("Memory quarantined for %s, backup=%s",
                        self.user_id, backup_id)
            return RecoveryResult(
                True, "quarantine", "memory",
                f"Memory quarantined and reset. Backup ID: {backup_id}",
                backup_id=backup_id,
            )

    # ── restore ───────────────────────────────────────────────────────

    def restore_history(self, backup_id: str) -> RecoveryResult:
        """Validate and atomically restore a History backup.

        *backup_id* must be a filename previously returned by
        :meth:`quarantine_history` or :meth:`list_history_backups`.
        Absolute paths and directory-traversal attempts are rejected.

        **Read, validate, and atomic replacement all happen within the
        user coordination lock** so concurrent recovery operations cannot
        race.
        """
        with self._coordinator.lock:
            return self._restore_history_locked(backup_id)

    def _restore_history_locked(self, backup_id: str) -> RecoveryResult:
        backup_path = self._resolve_backup(backup_id)

        # ── read and validate under lock ──────────────────────────
        try:
            data = read_json(backup_path, default=None)
        except Exception as exc:
            logger.warning("History backup unreadable for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(
                False, "restore", "history",
                "Backup is unreadable — active file unchanged",
            )

        if data is None:
            return RecoveryResult(
                False, "restore", "history",
                "Backup file not found or empty",
            )

        try:
            HistoryRepository.validate_schema(data)
        except CorruptHistoryError as exc:
            logger.warning("History backup schema invalid for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(
                False, "restore", "history",
                "Backup validation failed — active file unchanged",
            )

        # ── atomic replacement ─────────────────────────────────────
        write_json_atomic(self._history.path, data)
        logger.info("History restored for %s from backup=%s",
                    self.user_id, backup_id)
        return RecoveryResult(
            True, "restore", "history",
            f"History restored from backup {backup_id}",
        )

    def restore_memory(self, backup_id: str) -> RecoveryResult:
        """Validate and atomically restore a Memory backup.

        The backup must belong to the current user (enforced by
        *user_id* match).  Validation failures leave the active
        snapshot unchanged.

        **Read, validate, owner check, and atomic replacement all happen
        within the user coordination lock.**  The live MemoryManager is
        reloaded after a successful restore.
        """
        with self._coordinator.lock:
            return self._restore_memory_locked(backup_id)

    def _restore_memory_locked(self, backup_id: str) -> RecoveryResult:
        backup_path = self._resolve_backup(backup_id)

        # ── read and validate under lock ──────────────────────────
        try:
            data = read_json(backup_path, default=None)
        except Exception as exc:
            logger.warning("Memory backup unreadable for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(
                False, "restore", "memory",
                "Backup is unreadable — active file unchanged",
            )

        if data is None:
            return RecoveryResult(
                False, "restore", "memory",
                "Backup file not found or empty",
            )

        # Cross-user guard: the backup must declare the same user_id.
        if not isinstance(data, dict):
            return RecoveryResult(
                False, "restore", "memory",
                "Backup has invalid root type — active file unchanged",
            )
        backup_user = data.get("user_id")
        if not isinstance(backup_user, str):
            logger.warning("Memory backup user_id malformed for %s: %r",
                           self.user_id, backup_user)
            return RecoveryResult(
                False, "restore", "memory",
                "Backup ownership cannot be verified — active file unchanged",
            )
        if backup_user != self.user_id:
            logger.warning("Memory backup cross-user reject: %s tried to restore %s backup",
                           self.user_id, backup_user)
            return RecoveryResult(
                False, "restore", "memory",
                "Backup belongs to a different user; restore denied",
            )

        try:
            MemorySnapshotRepository.validate_schema(data)
        except CorruptMemorySnapshotError as exc:
            logger.warning("Memory backup schema invalid for %s: %s",
                           self.user_id, exc)
            return RecoveryResult(
                False, "restore", "memory",
                "Backup validation failed — active file unchanged",
            )

        # Rebuild from the validated backup before replacing the active file.
        # A rejected remote embedding must not overwrite the old snapshot.
        if getattr(self._memory_manager, "snapshot_restore_failed", None) is not None:
            candidate = MemorySnapshotRepository(backup_path, self.user_id)
            candidate.restore_to_manager(
                self._memory_manager, commit=lambda: write_json_atomic(self._memory.path, data),
            )
            return RecoveryResult(
                True, "restore", "memory",
                f"Memory restored from backup {backup_id}",
            )

        write_json_atomic(self._memory.path, data)

        # Reload the live MemoryManager so in-memory state matches the
        # restored file.  Clear stale in-memory state *without* saving
        # (which would overwrite the just-restored file), then load.
        if self._memory_manager is not None:
            for memory_module in self._memory_manager.memory_types.values():
                if hasattr(memory_module, "memories"):
                    memory_module.memories.clear()
            self._memory.restore_to_manager(self._memory_manager)

        logger.info("Memory restored for %s from backup=%s",
                    self.user_id, backup_id)
        return RecoveryResult(
            True, "restore", "memory",
            f"Memory restored from backup {backup_id}",
        )

    # ── listing ───────────────────────────────────────────────────────

    def list_history_backups(self) -> list[str]:
        """Return opaque backup identifiers for available History backups.

        Only backups belonging to this user are visible.  ``.tmp`` files
        from interrupted atomic writes are excluded.
        """
        return sorted(
            p.name for p in self._backup_dir.glob("history.json.corrupt-*")
            if not p.name.endswith(".tmp")
        )

    def list_memory_backups(self) -> list[str]:
        """Return opaque backup identifiers for available Memory backups.

        ``.tmp`` files from interrupted atomic writes are excluded.
        """
        return sorted(
            p.name for p in self._backup_dir.glob("*memor*.json.corrupt-*")
            if not p.name.endswith(".tmp")
        )

    # ── internal helpers ──────────────────────────────────────────────

    def _store_backup(self, source: Path) -> str:
        """Copy *source* into the user backup directory and remove the
        original.  Returns the opaque filename (backup ID).

        **Atomic write (Item 4):** copies to a temp file first, then
        atomically renames to the final destination.  If ``copy2`` is
        interrupted the temp file is cleaned up — no partial backup can
        ever appear in ``list_*_backups()``.

        **Never overwrites** an existing backup — if the destination
        already exists (extremely unlikely with UUID names), a new
        unique name is generated.
        """
        import os
        import shutil

        dest = self._backup_dir / source.name
        if dest.exists():
            # Collision-resistant: append another UUID segment.
            alt_name = f"{source.stem}-{uuid.uuid4().hex[:8]}{source.suffix or ''}"
            dest = self._backup_dir / alt_name

        # Write to a temp name first, then atomic rename.
        tmp_dest = dest.with_suffix(dest.suffix + ".tmp")
        try:
            shutil.copy2(source, tmp_dest)
            os.replace(tmp_dest, dest)  # atomic on same filesystem
        except Exception:
            # Clean up the temp file so no partial copy lingers.
            tmp_dest.unlink(missing_ok=True)
            raise

        source.unlink(missing_ok=True)
        return dest.name

    def _emergency_preserve(
        self, source: Path, label: str
    ) -> str | None:
        """Last-resort: save *source* into the user backup directory and
        remove the original.  Returns the backup_id, or None on failure.

        Used when both ``_store_backup`` and active restore fail —
        the staged backup is the **only** copy and must not be deleted.
        """
        import shutil

        dest = self._backup_dir / f"{source.name}.emergency-{label}-{uuid.uuid4().hex[:8]}"
        try:
            shutil.copy2(source, dest)
            source.unlink(missing_ok=True)
            logger.info("Emergency backup preserved for %s: %s",
                        self.user_id, dest.name)
            return dest.name
        except Exception:
            logger.critical(
                "Emergency backup FAILED for %s — last copy may be at %s",
                self.user_id, source, exc_info=True,
            )
            return None

    def _resolve_backup(self, backup_id: str) -> Path:
        """Resolve an opaque backup identifier to a file path within the
        user-scoped backup directory.

        Raises :exc:`ValueError` for traversal attempts and
        :exc:`FileNotFoundError` when the backup does not exist.
        """
        if not backup_id or "/" in backup_id or "\\" in backup_id:
            raise ValueError(f"Invalid backup identifier: {backup_id!r}")

        candidate = (self._backup_dir / backup_id).resolve()
        root = self._backup_dir.resolve()

        try:
            candidate.relative_to(root)
        except ValueError:
            raise ValueError(
                f"Backup identifier escapes backup directory: {backup_id!r}"
            ) from None

        if not candidate.exists():
            raise FileNotFoundError(f"Backup not found: {backup_id!r}")

        return candidate
