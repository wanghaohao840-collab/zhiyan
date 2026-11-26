from dataclasses import replace
import subprocess

import pytest
from qdrant_client import QdrantClient

from evals import rag_qdrant_parity
from evals.rag_qdrant_parity import (
    _loopback_port, build_qdrant, checked_chunk_ids, difference, prepared_vectors, ranked_hits, with_decoys,
)
from evals.rag_quality_dataset import GoldCase, GoldChunk
from evals.rag_quality_experiment import SimpleEmbedding, build_index


def test_parity_replays_same_vectors_and_checks_scope_and_source_metadata(tmp_path):
    case = GoldCase(
        "q-1", "en", "answerable", "parity-a", ("shared-doc",),
        "How long must the operator wait?", ("shared-doc_0",), ("57 minutes",), (),
    )
    chunk = GoldChunk(
        "shared-doc_0", case.namespace, "shared-doc", "Waiting Manual.pdf", 4,
        "The operator must wait 57 minutes.",
    )
    other = replace(
        chunk, id="outside-doc_0", document_id="outside-doc", content="The operator must wait 75 minutes.",
    )
    chunks = with_decoys([chunk, other], [case])
    documents, queries = prepared_vectors(chunks, [case], SimpleEmbedding())
    assert len(documents.vectors) == len(chunks) - 1  # identical across namespaces
    assert len(queries.vectors) == 1
    client = QdrantClient(":memory:")
    try:
        local = build_index(chunks, tmp_path, documents)
        for pipeline in local.values():
            pipeline.embedder = queries
        remote = build_qdrant(chunks, client, documents, queries)
        for pipelines in (local, remote):
            ids = checked_chunk_ids(pipelines, chunks)
            for mode in ("vector", "hybrid", "rerank", "mmr"):
                hits, _ = ranked_hits(pipelines[case.namespace], ids, case, mode)
                assert [item[0] for item in hits] == [chunk.id]
            probe = pipelines[f"{case.namespace}-outside"].get_document_chunks(chunk.document_id)
            assert probe[0]["metadata"]["page_number"] == 1004
    finally:
        client.close()


def test_parity_difference_requires_score_ties():
    assert difference([("a", 0.5), ("b", 0.5)], [("b", 0.5), ("a", 0.5)]) == "tie_order"
    assert difference([("a", 0.6), ("b", 0.5)], [("a", 0.6), ("c", 0.5)]) == "tie_at_cutoff"
    assert difference([("a", 0.6), ("b", 0.5)], [("b", 0.6), ("a", 0.5)]) == "unexplained"


@pytest.mark.parametrize("bindings", [
    {},
    {"6333/tcp": [{"HostIp": "0.0.0.0", "HostPort": "60000"}]},
    {"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": "6333"}, {"HostIp": "::", "HostPort": "6333"}]},
    {"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": "0"}]},
])
def test_loopback_rejects_nonexclusive_local_ports(bindings):
    with pytest.raises(ValueError):
        _loopback_port(bindings)


def test_loopback_uses_verified_port():
    assert _loopback_port({"6333/tcp": [{"HostIp": "127.0.0.1", "HostPort": "60439"}]}) == 60439


def test_loopback_start_timeout_cleans_up_owned_container(monkeypatch):
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    stops = []
    captured_label = None

    def docker(*args, env=None):
        nonlocal captured_label
        if args[:2] == ("context", "show"):
            return "desktop-linux"
        if args[:2] == ("context", "inspect"):
            return "npipe:////./pipe/dockerDesktopLinuxEngine"
        assert args[:2] == ("--context", "desktop-linux")
        command = args[2:]
        if command[:2] == ("image", "inspect"):
            return "sha256:mock-image"
        if command[0] == "run":
            captured_label = command[command.index("--label") + 1]
            assert "--pull=never" in command
            assert env["QDRANT__SERVICE__API_KEY"] not in command
            raise subprocess.TimeoutExpired("docker run", 45)
        if command[:2] == ("ps", "-q"):
            assert command[-1] == f"label={captured_label}"
            return "only-owned-container"
        if command[0] == "stop":
            stops.append(command[-1])
            return "only-owned-container"
        if command[:2] == ("ps", "-aq"):
            assert command[-1] == f"label={captured_label}"
            return ""
        pytest.fail(f"Unexpected test Docker operation {command[0]}")

    monkeypatch.setattr(rag_qdrant_parity, "_docker", docker)
    summary = {}
    with pytest.raises(subprocess.TimeoutExpired):
        with rag_qdrant_parity.loopback_qdrant(summary):
            pytest.fail("A timed out container must not yield a client")
    assert stops == ["only-owned-container"]
    assert summary["cleanup_verified"]
