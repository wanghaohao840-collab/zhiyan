"""Prepare all account-scoped legacy JSON RAG caches for one index cutover.

The caller holds the deployment maintenance lock, stops writers and protects
the data directory. The inventories and checkpoints contain original chunks.
The evidence and CLI output contain counts, identities, metrics and digests.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import TemporaryDirectory
import uuid

from app.rag_authority import AppOwnershipSource, checked_path, namespace_user
from app.rag_inventory import build_app_inventory
from deploy.embedding_probe import read_values
from evals.rag_embedding_acceptance import compare_runtimes, inspect_report, write_report
from evals.rag_retrieval_quality import QualityFailure, load_dataset
from hello_agents.memory.rag.candidate_rebuild import build_candidate, publish_json_candidate
from hello_agents.memory.rag.candidate_validation import validate_candidate
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure
from hello_agents.memory.rag.embedding_runtime import CHUNKING_BY_BACKEND, build_rag_embedding
from hello_agents.memory.rag.index_identity import IndexIdentity
from hello_agents.memory.rag.index_registry import IndexRecord, IndexRegistryError, IndexValidation
from hello_agents.memory.rag.json_index_cache import JsonIndexCache
from hello_agents.memory.rag.source_inventory import inspect_inventory
from hello_agents.memory.rag.source_json import JsonChunkSource
from hello_agents.memory.rag.source_records import SourceInventoryError, canonical, digest


BASE_COLLECTION = "pdf_learning_collection"
DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evals" / "data" / "bge_m3_retrieval.json"
_MIGRATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class JsonMigrationFailure(RuntimeError):
    """A JSON migration could not be prepared or verified."""


def _account_caches(root: Path):
    authority = AppOwnershipSource(root)
    with_documents = {owner for _, _, owner in authority.records()}
    database = checked_path(root / "app.db")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        user_ids = [namespace_user(f"pdf_{row[0]}") for row in db.execute("SELECT id FROM users ORDER BY id")]
    if not with_documents.issubset(user_ids):
        raise JsonMigrationFailure("source_changed")
    caches = {}
    for user_id in user_ids:
        user_root = checked_path(root / "users" / user_id, directory=True, missing=True)
        legacy = checked_path(user_root / "rag" / "rag_cache.json", missing=True)
        if legacy.exists():
            caches[user_id] = "cached"
        elif user_id in with_documents:
            raise JsonMigrationFailure("missing_source_cache")
        else:
            caches[user_id] = "initialized_empty" if user_root.exists() else "uninitialized"
    return authority.completed_token, caches


def _directory(path: Path) -> Path:
    checked_path(path, directory=True, missing=True)
    path.mkdir(parents=True, exist_ok=True)
    return checked_path(path, directory=True)


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path):
    return json.loads(checked_path(path).read_text(encoding="utf-8"))


def _source(path: Path, user_id: str, identity: IndexIdentity | None = None):
    return JsonChunkSource(
        path, namespace=f"pdf_{user_id}", identity=identity,
        collection=identity.physical_collection if identity else BASE_COLLECTION,
        dimension=identity.profile.dimension if identity else 384,
    )


def _temporary_inventory(root, inventories, source, destination=None):
    attempt = inventories / f".json-source-{uuid.uuid4().hex}.sqlite"
    try:
        summary = build_app_inventory(root, attempt, source=source)
        if destination is not None:
            os.replace(attempt, destination)
        return summary
    finally:
        attempt.unlink(missing_ok=True)


def candidate_from_evidence(path: Path | str) -> IndexRecord:
    """Read the receipt for use with begin_cutover while writers stay stopped."""
    try:
        raw = _read_json(Path(path))
        fingerprint = raw.pop("evidence_digest")
        record = IndexRecord.from_dict(raw["candidate_record"])
        if (fingerprint != digest(["json-embedding-migration-v1", raw])
                or record.identity.backend != "json" or record.state != "active"
                or record.source_index != BASE_COLLECTION):
            raise ValueError()
        return record
    except (ValueError, TypeError, KeyError, AttributeError, OSError,
            IndexRegistryError, SourceInventoryError):
        raise JsonMigrationFailure("evidence") from None


def prepare_json_migration(
    data_root: Path | str, *, migration_id: str, candidate_runtime,
    dataset: Path | str = DEFAULT_DATASET, quality_report: Path | str | None = None,
) -> IndexRecord:
    """Prepare and verify candidates without writing the registry or environment."""
    if (not _MIGRATION_ID.fullmatch(migration_id or "")
            or candidate_runtime.profile.chunking != CHUNKING_BY_BACKEND["json"]):
        raise JsonMigrationFailure("configuration")
    root = checked_path(data_root, directory=True)
    identity = IndexIdentity("json", BASE_COLLECTION, candidate_runtime.profile)
    registry_root = root / "vector_indexes" / "rag"
    inventories = _directory(registry_root / "inventories")
    work = _directory(registry_root / "json-migrations" / migration_id)
    plan_path = checked_path(work / "sources.json", missing=True)
    quality_path = checked_path(work / "quality.json", missing=True)
    evidence_path = checked_path(work / "evidence.json", missing=True)
    items = load_dataset(dataset)
    dataset_digest = digest(items)

    authority, accounts = _account_caches(root)
    inputs = {}
    for user_id, status in accounts.items():
        if status == "cached":
            legacy = root / "users" / user_id / "rag" / "rag_cache.json"
            source_path = checked_path(inventories / f"{migration_id}-{user_id}-json.sqlite", missing=True)
            source = _source(legacy, user_id)
            summary = (
                inspect_inventory(source_path) if source_path.exists()
                else _temporary_inventory(root, inventories, source, source_path)
            )
            inputs[user_id] = (legacy, source_path, summary)

    plan = {
        "identity": identity.to_dict(), "dataset_digest": dataset_digest,
        "authority": authority, "account_caches": digest(accounts),
        "source_inventories": digest({user_id: asdict(item[2]) for user_id, item in inputs.items()}),
    }
    if plan_path.exists():
        if _read_json(plan_path) != plan:
            raise JsonMigrationFailure("source_changed")
    else:
        _write_json(plan_path, plan)

    supplied_quality = None if quality_report is None else inspect_report(
        checked_path(quality_report), expected_fingerprint=identity.profile.fingerprint,
    )
    if supplied_quality is not None and supplied_quality.dataset_digest != dataset_digest:
        raise JsonMigrationFailure("quality_changed")
    if quality_path.exists():
        quality = inspect_report(quality_path, expected_fingerprint=identity.profile.fingerprint)
        if quality.dataset_digest != dataset_digest or (supplied_quality is not None and supplied_quality != quality):
            raise JsonMigrationFailure("quality_changed")
    else:
        quality = supplied_quality
        if quality is None:
            baseline = build_rag_embedding({}, backend="json")
            quality = compare_runtimes(items, baseline, candidate_runtime)
        write_report(quality_path, quality)

    reports = {}
    chunk_count = 0
    for user_id, (legacy, source_path, summary) in inputs.items():
        def verify_live_source():
            current = _temporary_inventory(root, inventories, _source(legacy, user_id))
            if current != summary:
                raise JsonMigrationFailure("source_changed")
            return current

        checkpoint = checked_path(work / f"{user_id}.sqlite", missing=True)
        cache = JsonIndexCache(legacy, identity, f"pdf_{user_id}")
        checked_path(cache.path, missing=True)
        candidate = build_candidate(
            checkpoint, migration_id=migration_id, inventory_path=source_path,
            inventory=summary, identity=identity, embedding=candidate_runtime,
            verify_live_source=verify_live_source, batch_size=candidate_runtime.batch_size,
        )
        candidate = publish_json_candidate(
            checkpoint, expected=candidate, identity=identity,
            legacy_path=legacy, rag_namespace=cache.rag_namespace,
        )
        with TemporaryDirectory(prefix="verify-", dir=work) as directory:
            validation = validate_candidate(
                Path(directory) / "validation.json", candidate_path=checkpoint, candidate=candidate,
                source_inventory_path=source_path, source_inventory=summary,
                target_inventory_path=Path(directory) / "target.sqlite",
                target_source=_source(cache.path, user_id, identity), verify_live_source=verify_live_source,
            )
        reports[user_id] = asdict(validation)
        chunk_count += validation.chunk_count

    for user_id, status in accounts.items():
        if status == "initialized_empty":
            legacy = root / "users" / user_id / "rag" / "rag_cache.json"
            _directory(legacy.parent)
            cache = JsonIndexCache(legacy, identity, f"pdf_{user_id}")
            checked_path(cache.path, missing=True)
            if not cache.path.exists():
                cache.write([])
            source = _source(cache.path, user_id, identity)
            if sum(1 for _ in source.records()):
                raise JsonMigrationFailure("unexpected_target_chunks")
            reports[user_id] = {"empty_cache_digest": source.completed_token}

    if _account_caches(root) != (authority, accounts):
        raise JsonMigrationFailure("source_changed")
    for user_id, (legacy, _, summary) in inputs.items():
        if _temporary_inventory(root, inventories, _source(legacy, user_id)) != summary:
            raise JsonMigrationFailure("source_changed")

    evidence = {
        "source_plan_digest": digest(plan), "validation_digest": digest(reports),
        "account_count": len(accounts), "legacy_cache_count": len(inputs),
        "initialized_empty_count": sum(status == "initialized_empty" for status in accounts.values()),
        "uninitialized_count": sum(status == "uninitialized" for status in accounts.values()),
        "quality": asdict(quality),
    }
    if evidence_path.exists():
        record = candidate_from_evidence(evidence_path)
    else:
        record = None
    stamp = record.validation.checked_at if record is not None else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    expected = IndexRecord(
        identity=identity, source_index=BASE_COLLECTION, migration_id=migration_id, state="active",
        validation=IndexValidation(
            passed=True, checked_at=stamp, chunk_count=chunk_count, scope_leaks=quality.candidate.scope_leaks,
            content_digest=digest([[user_id, row.get("content_digest")] for user_id, row in sorted(reports.items())]),
            recall_at_5=quality.candidate.recall_at_5, mrr_at_5=quality.candidate.mrr_at_5,
        ),
    )
    evidence["candidate_record"] = expected.to_dict()
    evidence["evidence_digest"] = digest(["json-embedding-migration-v1", evidence])
    if record is None:
        _write_json(evidence_path, evidence)
    elif _read_json(evidence_path) != evidence:
        raise JsonMigrationFailure("evidence_changed")
    return expected


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Prepare local JSON RAG embeddings with writers stopped")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--migration-id", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--quality-report", type=Path)
    args = parser.parse_args(argv)
    try:
        values = read_values(args.env_file)
        values["RAG_EMBEDDING_PROVIDER"] = "siliconflow"
        record = prepare_json_migration(
            args.data_root, migration_id=args.migration_id, dataset=args.dataset, quality_report=args.quality_report,
            candidate_runtime=build_rag_embedding(values, backend="json"),
        )
        print(canonical({"status": "validated", "index_record": record.to_dict()}))
        return 0
    except EmbeddingFailure as exc:
        print(canonical({"status": "failed", "code": exc.code, "http_status": exc.status_code,
                         "retryable": exc.retryable}))
    except Exception as exc:
        code = str(exc) if isinstance(exc, (JsonMigrationFailure, QualityFailure)) else getattr(exc, "code", None)
        safe = {"configuration", "source_changed", "missing_source_cache", "quality_changed", "gate",
                "unexpected_target_chunks", "evidence", "evidence_changed", "authority_orphan",
                "authority_database_busy", "authority_path", "incomplete_partition", "target_exists"}
        print(canonical({"status": "failed", "code": code if code in safe else "migration"}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
