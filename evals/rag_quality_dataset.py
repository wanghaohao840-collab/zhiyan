
"""Gold cases for the scoped retrieval and answer benchmark."""

from dataclasses import dataclass
import json
from pathlib import Path


DEFAULT_DATASET = Path(__file__).with_name("data") / "rag_quality_gold.json"


@dataclass(frozen=True)
class GoldChunk:
    id: str
    namespace: str
    document_id: str
    file_name: str
    page_number: int
    content: str


@dataclass(frozen=True)
class GoldCase:
    id: str
    language: str
    category: str
    namespace: str
    scope: tuple[str, ...]
    question: str
    evidence: tuple[str, ...]
    facts: tuple[str, ...]
    forbidden: tuple[str, ...]


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ) or len(value) != len(set(value)):
        raise ValueError(f"Invalid {label}")
    return tuple(value)


def load_gold_dataset(path: Path = DEFAULT_DATASET) -> tuple[list[GoldChunk], list[GoldCase]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1:
        raise ValueError("Unknown gold dataset schema")
    chunks = [GoldChunk(**item) for item in raw["chunks"]]
    cases = [
        GoldCase(**{
            **item,
            **{key: _strings(item[key], key) for key in ("scope", "evidence", "facts", "forbidden")},
        })
        for item in raw["cases"]
    ]
    by_id = {chunk.id: chunk for chunk in chunks}
    if len(by_id) != len(chunks) or len({case.id for case in cases}) != len(cases):
        raise ValueError("Duplicate gold IDs")
    scopes = {(chunk.namespace, chunk.document_id) for chunk in chunks}
    for chunk in chunks:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (chunk.id, chunk.namespace, chunk.document_id, chunk.file_name, chunk.content)
        ) or type(chunk.page_number) is not int or chunk.page_number < 1:
            raise ValueError(f"Invalid golden chunk {chunk.id}")

    for case in cases:
        answerable = case.category in {"answerable", "distractor"}
        if (
            not all(isinstance(value, str) and value.strip() for value in (case.id, case.question))
            or case.language not in {"zh", "en"}
            or case.category not in {"answerable", "distractor", "unanswerable", "scope_isolated"}
            or not 1 <= len(case.scope) <= 10
            or any((case.namespace, doc) not in scopes for doc in case.scope)
            or bool(case.evidence) != answerable
            or bool(case.facts) != answerable
            or any(
                id_ not in by_id
                or by_id[id_].namespace != case.namespace
                or by_id[id_].document_id not in case.scope
                for id_ in case.evidence
            )
        ):
            raise ValueError(f"Invalid golden case {case.id}")
    return chunks, cases
