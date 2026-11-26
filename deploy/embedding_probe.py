# deploy/embedding_probe.py
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import time

from dotenv.parser import parse_stream

from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding
from hello_agents.memory.rag.embedding_profile import EmbeddingFailure, EmbeddingSettings


def read_values(path: Path) -> dict[str, str | None]:
    try:
        text = path.read_text(encoding="utf-8-sig")
        values: dict[str, str | None] = {}
        # Consume parser bindings before duplicate assignments collapse into a
        # dict. This preserves quoted/multiline syntax without interpolation or
        # dotenv's warn-and-continue handling of malformed configuration.
        for binding in parse_stream(io.StringIO(text)):
            if binding.error:
                raise ValueError()
            key = binding.key
            if key is None or not key.startswith("RAG_EMBEDDING_"):
                continue
            if key in values:
                raise ValueError()
            values[key] = binding.value
        return values
    except (OSError, UnicodeError, ValueError):
        raise EmbeddingFailure("environment_file") from None


def main(argv=None, *, client_factory=SiliconFlowEmbedding) -> int:
    parser = argparse.ArgumentParser(description="Check candidate RAG embedding settings")
    parser.add_argument("--env-file", required=True, type=Path)
    parser.add_argument("--probe", action="store_true",
                        help="Send fixed public test sentences to the candidate provider")
    args = parser.parse_args(argv)
    try:
        values = read_values(args.env_file)
        settings = EmbeddingSettings.from_env(values, candidate=True)
        report = {"status": "configuration_valid", "activation": "unchanged",
                  "key_configured": bool(settings.api_key),
                  "model": settings.profile.model,
                  "dimension": settings.profile.dimension,
                  "fingerprint": settings.profile.fingerprint}
        if args.probe:
            started = time.monotonic()
            client = client_factory(settings)
            documents = client.embed_documents([
                "知研公开连通性测试：水在标准大气压下约一百度沸腾。",
                "Public connectivity test: plants use sunlight for photosynthesis.",
            ])
            query = client.embed_query("What do plants use sunlight for?")
            report.update(status="probe_passed", vector_count=len(documents) + 1,
                          query_dimension=len(query),
                          elapsed_seconds=round(time.monotonic() - started, 3))
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except EmbeddingFailure as exc:
        failure = {"status": "failed", "code": exc.code,
                   "http_status": exc.status_code, "retryable": exc.retryable}
        if exc.status_code == 402:
            failure["reason"] = "provider_billing"
            failure["action"] = "check_account_balance_or_key"
        print(json.dumps(failure))
        return 1
    except Exception:
        # The CLI boundary cannot emit a traceback containing request/file secrets.
        print(json.dumps({"status": "failed", "code": "internal"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
