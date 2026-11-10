"""Personal-memory embedding configuration, separate from document RAG."""

from __future__ import annotations

import hashlib
import os
from typing import Mapping, Sequence

from hello_agents.memory.embedding import get_text_embedder
from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding
from hello_agents.memory.rag.embedding_profile import EmbeddingSettings, normalize_vector


class BgeM3MemoryEmbedding:
    """Adapt the existing bounded client to Memory's encode interface."""

    dimension = 1024

    def __init__(self, settings: EmbeddingSettings, *, transport=None):
        self.client = SiliconFlowEmbedding(settings, transport=transport)
        self.fingerprint = hashlib.sha256(
            (settings.profile.fingerprint + ":memory-char1500-mean-v1").encode()
        ).hexdigest()

    def encode(self, texts: str | Sequence[str]):
        if isinstance(texts, str):
            return self._encode_one(texts)
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text: str) -> list[float]:
        # Bound UTF-8 size even for long notes, retaining all nonblank content.
        parts = [text[start:start + 1500] for start in range(0, len(text), 1500)]
        vectors = self.client.embed_documents([part for part in parts if part.strip()] or [text])
        pooled = [sum(column) / len(vectors) for column in zip(*vectors)]
        return normalize_vector(pooled, self.dimension)


def get_memory_embedder(*, values: Mapping[str, str | None] | None = None,
                        transport=None):
    values = os.environ if values is None else values
    # Credential/model defaults may be shared, activation is explicitly per use case.
    settings_values = {
        key: value for key, value in values.items() if key.startswith("RAG_EMBEDDING_")
    }
    settings_values.update({
        "RAG_EMBEDDING_" + key.removeprefix("MEMORY_EMBEDDING_"): value
        for key, value in values.items() if key.startswith("MEMORY_EMBEDDING_")
    })
    settings_values["RAG_EMBEDDING_PROVIDER"] = values.get("MEMORY_EMBEDDING_PROVIDER", "simple")
    settings = EmbeddingSettings.from_env(settings_values)
    if settings.profile.provider == "simple":
        return get_text_embedder()
    return BgeM3MemoryEmbedding(settings, transport=transport)


def memory_vector_collection(collection: str, embedder) -> str:
    fingerprint = getattr(embedder, "fingerprint", None)
    if fingerprint:
        return f"{collection}__bge_m3_{fingerprint[:16]}"
    return collection
