"""Explicit local command for durable trusted-user initialization or replay."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
from pathlib import Path
import re
import sys
from typing import Sequence
from uuid import UUID
import warnings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import boto3
from botocore.config import Config
from qdrant_client import QdrantClient

from app.object_store import S3ObjectStore
from app.postgres import PostgresDatabase
from app.trusted_user_initialization import (
    InitializedUser, InitializationError, InitializationOutcomeUnknown,
    initialize_trusted_user,
)
from app.trusted_user_initialization_settings import TrustedUserInitializationSettings
from hello_agents.memory.storage.vector_store import QdrantVectorStore


_SERVICE_CODES = frozenset({
    "busy", "expired", "timeout", "preflight_failed", "invalid_request",
    "configuration", "unavailable", "conflict", "corrupt", "mismatch", "failed",
})


class _InputFailure(Exception):
    pass


class _HelpRequested(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _InputFailure() from None

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise _HelpRequested() from None
        raise _InputFailure() from None


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            raise _InputFailure()
        setattr(namespace, self.dest, values)


def _arguments(argv: Sequence[str] | None) -> tuple[UUID, str]:
    parser = _Parser(description="Initialize or replay a saved trusted-user operation",
                     allow_abbrev=False)
    parser.add_argument("--operation-id", required=True, action=_Once)
    parser.add_argument("--username", required=True, action=_Once)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                        args.operation_id):
        raise _InputFailure()
    try:
        operation_id = UUID(args.operation_id)
    except (ValueError, AttributeError, TypeError):
        raise _InputFailure() from None
    if str(operation_id) != args.operation_id:
        raise _InputFailure()
    return operation_id, args.username


def _password() -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            first = getpass.getpass("Password: ")
            second = getpass.getpass("Repeat password: ")
    except (EOFError, KeyboardInterrupt, getpass.GetPassWarning):
        raise _InputFailure() from None
    if first != second:
        raise _InputFailure()
    return first


def _preflight(settings: TrustedUserInitializationSettings) -> None:
    # These are individual request limits, not a deadline for the command.
    raw_s3 = None
    raw_qdrant = None
    error: BaseException | None = None
    try:
        raw_s3 = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(connect_timeout=5, read_timeout=10,
                          retries={"max_attempts": 2},
                          s3={"addressing_style": "path"}),
        )
        S3ObjectStore(raw_s3, settings.s3_bucket).check_ready()
        raw_qdrant = QdrantClient(url=settings.qdrant_url,
                                  api_key=settings.qdrant_api_key, timeout=10,
                                  check_compatibility=False)
        vectors = QdrantVectorStore(client=raw_qdrant, retry_delays=())
        for identity in (settings.rag_identity, settings.episode_identity):
            vectors.require_collection(identity.physical_collection,
                                       identity.profile.dimension,
                                       identity.profile.distance)
    except BaseException as caught:
        error = caught
    finally:
        for client in (raw_qdrant, raw_s3):
            if client is not None:
                try:
                    client.close()
                except BaseException as caught:
                    if error is None or (isinstance(error, Exception)
                                         and not isinstance(caught, Exception)):
                        error = caught
    if error is not None:
        raise error


def _close_database(database: PostgresDatabase | None, opened: bool) -> bool:
    if database is None:
        return False
    failed = False
    if opened:
        try:
            database.close()
        except BaseException:
            failed = True
            try:
                database._pool.close()
            except BaseException:
                failed = True
    else:
        # PostgresDatabase.close() is a no-op if open() failed halfway.
        try:
            database._pool.close()
        except BaseException:
            failed = True
    return failed


def _run(argv: Sequence[str] | None) -> int:
    try:
        operation_id, username = _arguments(argv)
    except _HelpRequested:
        return 0
    except _InputFailure:
        print("invalid_input", file=sys.stderr)
        return 2

    try:
        settings = TrustedUserInitializationSettings.from_env()
    except BaseException:
        print("configuration", file=sys.stderr)
        return 2
    try:
        password = _password()
    except BaseException:
        print("invalid_input", file=sys.stderr)
        return 2

    database = None
    result: InitializedUser | None = None
    code: str | None = None
    entered_service = False
    opened = False
    try:
        database = PostgresDatabase(settings.database_url,
                                    min_size=settings.pg_pool_min,
                                    max_size=settings.pg_pool_max,
                                    timeout=10.0)
        database.open()
        opened = True
        called = False

        def preflight() -> None:
            nonlocal called
            if called:
                raise RuntimeError("preflight_called_twice")
            called = True
            _preflight(settings)

        entered_service = True
        candidate = initialize_trusted_user(database, operation_id, username,
                                            password, settings, preflight=preflight)
        if isinstance(candidate, InitializedUser):
            result = candidate
        else:
            code = "unknown"
    except InitializationOutcomeUnknown:
        code = "unknown" if entered_service else "unavailable"
    except InitializationError as caught:
        if entered_service:
            code = caught.code if caught.code in _SERVICE_CODES else "unknown"
        else:
            code = "unavailable"
    except BaseException:
        code = "unknown" if entered_service else "unavailable"

    cleanup_failed = _close_database(database, opened)
    if result is not None:
        print(json.dumps({
            "operation_id": str(operation_id),
            "user_id": result.user_id,
            "durable_initialized": True,
            "replayed": result.replayed,
            "next_step": "Configure the trusted user allowlist separately.",
        }, separators=(",", ":")))
        if cleanup_failed:
            print("cleanup_failed", file=sys.stderr)
        return 0
    if code == "unknown":
        print("unknown", operation_id, file=sys.stderr)
    else:
        print(code or "failed", file=sys.stderr)
    if cleanup_failed:
        print("cleanup_failed", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    previous_disable = logging.root.manager.disable
    logging.disable(sys.maxsize)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _run(argv)
    finally:
        logging.disable(previous_disable)


if __name__ == "__main__":
    # Keep native pool diagnostics suppressed until this dedicated CLI exits.
    logging.disable(sys.maxsize)
    raise SystemExit(main())
