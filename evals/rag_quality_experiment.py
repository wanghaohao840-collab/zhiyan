"""Run scoped retrieval, answer and citation comparisons against a fixed corpus."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from hashlib import sha256
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

from evals.rag_quality_dataset import DEFAULT_DATASET, load_gold_dataset
from evals.rag_quality_report import write_report
from evals.rag_quality_scoring import extract_answer_body, score_answer, score_citations
from hello_agents.memory.embedding import SimpleEmbedding
from hello_agents.memory.rag.pipeline import SimpleRAGPipeline
from hello_agents.tools.builtin.rag_tool import RAGTool


@dataclass(frozen=True)
class Variant:
    name: str
    retrieval_mode: str = "vector"
    use_mmr: bool = False
    use_rerank: bool = False
    min_score: float = 0.12
    cite: bool = True
    generate: bool = True
    verify_evidence: bool = False
    evidence_strategy: str = "two_pass"

    def search_options(self):
        return {
            "retrieval_mode": self.retrieval_mode, "use_mmr": self.use_mmr,
            "use_rerank": self.use_rerank, "min_score": self.min_score,
        }


BASELINE = Variant("vector")
HYBRID = Variant("hybrid", "hybrid")
RERANK = Variant("hybrid_rerank", "hybrid", use_rerank=True)
NO_CITATION_INSTRUCTION = Variant("hybrid_rerank_control", "hybrid", use_rerank=True, cite=False)
VARIANTS = (
    Variant("vector_no_threshold", min_score=0.0, generate=False),
    BASELINE, HYBRID, Variant("hybrid_mmr", "hybrid", use_mmr=True), RERANK,
    Variant("hybrid_rerank_mmr", "hybrid", use_mmr=True, use_rerank=True),
    NO_CITATION_INSTRUCTION,
)
# Declared before evaluating. Covers both languages, near misses and out-of-scope evidence.
LIVE_IDS = (
    "atlas-cn_0-qa", "quartz-cn_1-qa", "cedar-en_1-qa", "fjord-en_2-qa",
    "missing-1", "missing-3", "missing-6", "missing-7",
)
LIVE_VARIANTS = (BASELINE, HYBRID, RERANK, NO_CITATION_INSTRUCTION)

_CITATION_REQUIREMENT = "只能基于资料回答，并使用资料中的稳定引用 ID 标注依据。"
_SOURCE_BLOCK = re.compile(r"(?m)^\[(S-[a-f0-9]{12})(?: \| [^\n]*)?\]\n([^\n]+)")


class TrialGenerator:
    context_window_tokens = 8192

    def __init__(self, generate, cite):
        self.generate_answer = generate
        self.cite = cite
        self.error = None
        self.call_ms = []

    def generate(self, prompt):
        started = perf_counter()
        try:
            return self._generate(prompt)
        finally:
            self.call_ms.append((perf_counter() - started) * 1000)

    def _generate(self, prompt):
        self.error = None
        if not self.cite:
            if _CITATION_REQUIREMENT not in prompt:
                raise ValueError("Citation instruction changed")
            prompt = prompt.replace(_CITATION_REQUIREMENT, "只能基于资料回答。", 1)
        if self.generate_answer is None:
            # This generator sees the prompt only. It extracts the first passage.
            match = _SOURCE_BLOCK.search(prompt.split("\n资料：\n", 1)[1])
            if match is None:
                return "资料中没有提到。"
            id_, passage = match.groups()
            return f"{passage} [{id_}]" if self.cite else passage
        try:
            answer = self.generate_answer(prompt)
            if not answer.strip():
                self.error = "empty_generation"
            return answer
        except Exception as exc:
            self.error = provider_error(exc)
            return ""


def provider_error(exc):
    status = getattr(exc, "status_code", None)
    return f"http_{status}" if isinstance(status, int) else type(exc).__name__


def build_index(chunks, directory, embedder):
    get_document_vectors = getattr(embedder, "encode_documents", None)
    build_embedder = embedder
    if get_document_vectors is not None:
        contents = list(dict.fromkeys(chunk.content for chunk in chunks))
        vectors = get_document_vectors(contents)

        class PreparedDocumentVectors:
            dimension = embedder.dimension

            def encode(self, text):
                return indexed_vectors[text]

        indexed_vectors = dict(zip(contents, vectors, strict=True))
        build_embedder = PreparedDocumentVectors()

    pipelines = {}
    for chunk in chunks:
        if chunk.namespace not in pipelines:
            pipeline = SimpleRAGPipeline(
                rag_namespace=chunk.namespace,
                cache_path=str(directory / f"{len(pipelines)}.json"),
            )
            pipeline.embedder = build_embedder
            pipeline.dimension = embedder.dimension
            pipelines[chunk.namespace] = pipeline
        pipeline = pipelines[chunk.namespace]
        result = pipeline.add_text(
            chunk.content, document_id=chunk.document_id, replace_existing=False, save_cache=False,
            metadata={"file_name": chunk.file_name, "page_number": chunk.page_number},
        )
        if not result["success"]:
            raise RuntimeError(f"Could not index {chunk.id}")

    for doc in {(chunk.namespace, chunk.document_id) for chunk in chunks}:
        namespace, document_id = doc
        indexed = pipelines[namespace].get_document_chunks(document_id)
        expected = [chunk for chunk in chunks if (chunk.namespace, chunk.document_id) == doc]
        if [(item["id"], item["content"]) for item in indexed] != [
            (chunk.id, chunk.content) for chunk in expected
        ]:
            raise ValueError(f"Fixture chunk IDs or contents changed for {document_id}")
    for pipeline in pipelines.values():
        pipeline.embedder = embedder
    return pipelines


def evaluate_case(pipeline, case, variant, *, generate=None, repeats=1):
    options = {
        "query": case.question, "document_ids": list(case.scope), "limit": 5,
        **variant.search_options(),
    }
    if repeats < 0 or (repeats == 0 and not variant.generate):
        raise ValueError("Retrieval-only measurements must run search at least once")
    search_times = []
    hits = []
    for _ in range(repeats):
        started = perf_counter()
        hits = pipeline.search(**options)
        search_times.append((perf_counter() - started) * 1000)

    if variant.generate:
        generator, result, qa_ms, qa_retrieval_ms, hits = _measure_qa(
            pipeline, case, variant, options, generate,
        )
        if repeats == 0:
            search_times = qa_retrieval_ms
    ranks = {
        hit["id"]: rank for rank, hit in enumerate(hits, 1)
        if hit["metadata"].get("rag_namespace") == case.namespace
        and hit["metadata"].get("document_id") in case.scope
    }
    row = {
        "case": case.id, "language": case.language, "category": case.category,
        "variant": variant.name, "answerable": bool(case.evidence),
        "question": case.question, "scope": case.scope, "gold_facts": case.facts,
        "gold_chunks": case.evidence, "ranked_chunks": [hit["id"] for hit in hits],
        "scope_leaks": len(hits) - len(ranks), "retrieval_ms": search_times,
        "first_gold_rank": min((ranks[id_] for id_ in case.evidence if id_ in ranks), default=None),
        "gold_recall": (
            len(ranks.keys() & set(case.evidence)) / len(case.evidence) if case.evidence else None
        ),
    }
    if not variant.generate:
        return row

    body = extract_answer_body(result.message)
    sources = result.data.get("sources", [])
    error = generator.error or (None if body else result.error_code or "missing_answer")
    row.update(
        answer=body, qa_ms=qa_ms, generation_error=error,
        qa_retrieval_ms=qa_retrieval_ms, llm_call_ms=generator.call_ms,
        answer_scores=score_answer(body, case) if not error else None,
        citations=score_citations(body, sources, hits, case, pipeline),
        sources=sources,
        evidence_status=result.data.get("evidence_status"),
        evidence_error=result.data.get("evidence_error"),
    )
    row["scope_leaks"] += sum(source.get("document_id") not in case.scope for source in sources)
    return row


def _measure_qa(pipeline, case, variant, options, generate):
    qa_retrieval_ms = []
    hits = []

    def timed_search(**kwargs):
        nonlocal hits
        started = perf_counter()
        try:
            hits = pipeline.search(**kwargs)
            return hits
        finally:
            qa_retrieval_ms.append((perf_counter() - started) * 1000)

    # Use only the experiment's index. RAGTool's constructor may open a configured live index.
    tool = RAGTool.__new__(RAGTool)
    tool.rag_namespace = case.namespace
    tool._pipelines = {case.namespace: SimpleNamespace(search=timed_search)}
    tool.graph_service = None
    tool.qdrant_api_key = None
    generator = TrialGenerator(generate, variant.cite)
    tool.llm = generator
    started = perf_counter()
    result = tool.execute_result(
        "ask", mode="joint", graph_mode="off", verify_evidence=variant.verify_evidence,
        evidence_strategy=variant.evidence_strategy, **options,
    )
    return generator, result, (perf_counter() - started) * 1000, qa_retrieval_ms, hits


class RemoteEmbedder:
    def __init__(self, client):
        self.client = client
        self.dimension = client.profile.dimension

    def encode(self, text):
        return self.client.embed_query(text)

    def encode_documents(self, texts):
        return self.client.embed_documents(texts)


def configure_embedding(env_file):
    if env_file is None:
        return None, {"status": "unconfigured"}
    from deploy.embedding_probe import read_values
    from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding
    from hello_agents.memory.rag.embedding_profile import EmbeddingSettings

    try:
        settings = EmbeddingSettings.from_env(read_values(env_file), candidate=True)
        client = SiliconFlowEmbedding(settings)
        client.embed_query("Public RAG benchmark embedding check.")
        return RemoteEmbedder(client), {
            "status": "available", "model": client.profile.model,
            "revision": client.profile.revision, "fingerprint": client.profile.fingerprint,
        }
    except Exception as exc:
        return None, {"status": "unavailable", "reason": provider_error(exc)}


def configure_chat(env_file, stack, model_override=None, *, max_tokens=1536):
    if env_file is None:
        return None, {"status": "unconfigured"}
    from dotenv import dotenv_values
    import httpx
    from openai import OpenAI

    try:
        values = dotenv_values(env_file, interpolate=False)
        key = values.get("LLM_API_KEY") or values.get("OPENAI_API_KEY") or values.get("DEEPSEEK_API_KEY")
        if not key:
            return None, {"status": "unconfigured"}
        model = model_override or values.get("LLM_MODEL_ID") or values.get("OPENAI_MODEL") or "deepseek-chat"
        base = values.get("LLM_BASE_URL") or values.get("OPENAI_BASE_URL") or "https://api.deepseek.com/v1"
        transport = stack.enter_context(httpx.Client(trust_env=False, timeout=45))
        client = OpenAI(api_key=key, base_url=base, http_client=transport, max_retries=0)

        def generate(prompt):
            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                temperature=0, max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""

        if not generate("Reply with the word OK.").strip():
            return None, {"status": "unavailable", "reason": "empty_probe"}
        return generate, {"status": "available", "model": model, "temperature": 0, "max_tokens": max_tokens}
    except Exception as exc:
        return None, {"status": "unavailable", "reason": provider_error(exc)}


def run_trials(pipelines, cases, variants, *, generate=None, repeats=1, workers=1):
    jobs = []
    for i, case in enumerate(cases):
        # Rotate group order to reduce the effect of warm-up and temporal drift.
        for variant in variants[i % len(variants):] + variants[:i % len(variants)]:
            jobs.append((case, variant))

    def run(job):
        case, variant = job
        return evaluate_case(
            pipelines[case.namespace], case, variant, generate=generate, repeats=repeats,
        )

    if workers == 1:
        return [run(job) for job in jobs]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(run, jobs))


def main(argv=None):
    from contextlib import ExitStack

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--mode", choices=("offline", "auto", "real"), default="offline")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--embedding-env-file", type=Path)
    parser.add_argument("--chat-env-file", type=Path)
    parser.add_argument("--chat-model")
    args = parser.parse_args(argv)

    chunks, cases = load_gold_dataset(args.dataset)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    providers = {"embedding": {"status": "not_probed"}, "chat": {"status": "not_probed"}}
    tracks = {}
    with TemporaryDirectory(prefix="rag-quality-") as temp, ExitStack() as stack:
        simple_index = build_index(chunks, Path(temp) / "offline", SimpleEmbedding())
        if args.mode != "real":
            tracks["offline"] = run_trials(simple_index, cases, VARIANTS, repeats=5)
        metadata = {"dataset_sha256": sha256(args.dataset.read_bytes()).hexdigest(), "providers": providers}
        write_report(args.output_dir, metadata, tracks)
        if args.mode != "offline":
            remote, providers["embedding"] = configure_embedding(args.embedding_env_file)
            chat, providers["chat"] = configure_chat(args.chat_env_file, stack, args.chat_model)
            if remote and chat:
                remote_index = build_index(chunks, Path(temp) / "real", remote)
                tracks["real_full"] = run_trials(remote_index, cases, VARIANTS, generate=chat)
            elif args.mode == "auto" and chat:
                live_cases = [case for case in cases if case.id in LIVE_IDS]
                if live_cases:
                    tracks["mixed_real_chat"] = run_trials(
                        simple_index, live_cases, LIVE_VARIANTS, generate=chat, workers=3,
                    )
        write_report(args.output_dir, metadata, tracks)

    invalid = any(
        row["scope_leaks"] or row.get("generation_error")
        for rows in tracks.values() for row in rows
    )
    print(args.output_dir / "report.md")
    return int(invalid or (args.mode == "real" and "real_full" not in tracks))


if __name__ == "__main__":
    raise SystemExit(main())
