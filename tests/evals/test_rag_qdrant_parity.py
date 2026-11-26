from dataclasses import replace

from qdrant_client import QdrantClient

from evals.rag_qdrant_parity import (
    build_qdrant, checked_chunk_ids, difference, prepared_vectors, ranked_hits, with_decoys,
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
