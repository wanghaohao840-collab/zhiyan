"""Source-grounded learning-card and exercise draft generation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unicodedata import normalize


class LearningGenerationError(ValueError):
    """Raised when grounded learning drafts cannot be generated safely."""


class _RepairableJSONError(LearningGenerationError):
    """JSON syntax and root-shape failures eligible for one repair call."""


class LearningGenerationService:
    def __init__(self, rag_tool, llm):
        self.rag_tool = rag_tool
        self.llm = llm

    def generate_cards(
        self,
        document_id,
        document_name,
        count,
        existing_fronts=(),
    ) -> list[dict]:
        document_id, document_name, count = self._validated_request(
            document_id, document_name, count
        )
        sources = self._retrieve_sources(
            document_id,
            document_name,
            count,
            query="提取适合制作学习卡片的关键概念、事实与关系",
        )
        prompt = self._cards_prompt(count, sources)
        items = self._generate_json(prompt, "cards")
        if len(items) != count:
            raise LearningGenerationError(
                f"card count must be exactly {count}, got {len(items)}"
            )

        known = set(self._existing_values(existing_fronts, "existing_fronts"))
        drafts = []
        for raw_item in items:
            item = self._card(raw_item, sources)
            key = _normalized(item["front"])
            if key in known:
                raise LearningGenerationError("duplicate card front")
            known.add(key)
            drafts.append(item)
        return drafts

    def generate_exercises(
        self,
        document_id,
        document_name,
        count,
        kind="mixed",
        existing_prompts=(),
    ) -> list[dict]:
        document_id, document_name, count = self._validated_request(
            document_id, document_name, count
        )
        if kind not in {"mixed", "multiple_choice", "short_answer"}:
            raise LearningGenerationError(
                "kind must be mixed, multiple_choice, or short_answer"
            )
        sources = self._retrieve_sources(
            document_id,
            document_name,
            count,
            query="提取适合生成练习题的关键知识、因果关系与易错点",
        )
        prompt = self._exercises_prompt(count, kind, sources)
        items = self._generate_json(prompt, "exercises")
        if len(items) != count:
            raise LearningGenerationError(
                f"exercise count must be exactly {count}, got {len(items)}"
            )

        known = set(self._existing_values(existing_prompts, "existing_prompts"))
        drafts = []
        for raw_item in items:
            item = self._exercise(raw_item, sources)
            key = _normalized(item["prompt"])
            if key in known:
                raise LearningGenerationError("duplicate exercise prompt")
            known.add(key)
            drafts.append(item)

        self._validate_exercise_distribution(drafts, count, kind)
        return drafts

    @staticmethod
    def _validated_request(document_id, document_name, count) -> tuple[str, str, int]:
        document_id = _required_string(document_id, "document_id")
        document_name = _required_string(document_name, "document_name")
        if not _is_int(count) or count not in {3, 5, 10}:
            raise LearningGenerationError("count must be 3, 5, or 10")
        return document_id, document_name, count

    @staticmethod
    def _existing_values(values, name: str) -> list[str]:
        if isinstance(values, (str, bytes)):
            raise LearningGenerationError(f"{name} must contain strings")
        try:
            items = list(values)
        except TypeError as exc:
            raise LearningGenerationError(f"{name} must contain strings") from exc
        if not all(isinstance(value, str) and value.strip() for value in items):
            raise LearningGenerationError(f"{name} must contain non-empty strings")
        return [_normalized(value) for value in items]

    def _retrieve_sources(
        self,
        document_id: str,
        document_name: str,
        count: int,
        *,
        query: str,
    ) -> dict[str, dict]:
        limit = min(30, max(10, count * 3))
        try:
            result = self.rag_tool.execute_result(
                "search",
                document_id=document_id,
                query=query,
                limit=limit,
            )
        except Exception as exc:
            raise LearningGenerationError("RAG search failed") from exc

        data = getattr(result, "data", None)
        if not getattr(result, "success", False) or not isinstance(data, dict):
            raise LearningGenerationError("RAG search failed")
        results = data.get("results")
        if not isinstance(results, list):
            raise LearningGenerationError("RAG search returned invalid results")

        unique_hits = []
        seen_chunk_ids = set()
        for raw_hit in results:
            if not isinstance(raw_hit, dict):
                raise LearningGenerationError("RAG search returned an invalid hit")
            metadata = raw_hit.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("document_id") != document_id:
                raise LearningGenerationError(
                    "RAG evidence belongs to a missing or different document"
                )
            chunk_id = raw_hit.get("chunk_id")
            content = raw_hit.get("content")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                raise LearningGenerationError("RAG evidence is missing chunk_id")
            if not isinstance(content, str) or not content.strip():
                raise LearningGenerationError("RAG evidence content is empty")
            if chunk_id in seen_chunk_ids:
                continue
            seen_chunk_ids.add(chunk_id)
            unique_hits.append((chunk_id, content, metadata))

        if not unique_hits:
            raise LearningGenerationError("no grounded RAG evidence was found")

        sources = {}
        for index, (chunk_id, content, metadata) in enumerate(unique_hits, start=1):
            source_id = f"SRC-{index}"
            is_pdf = _is_pdf_source(metadata)
            sources[source_id] = {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "document_name": document_name,
                "page_number": _optional_int(metadata.get("page_number"))
                if is_pdf
                else None,
                "source_position": None
                if is_pdf
                else _optional_int(metadata.get("source_position")),
                "preview": content[:240],
                "_content": content,
            }
        return sources

    @staticmethod
    def _cards_prompt(count: int, sources: dict[str, dict]) -> str:
        schema = {
            "cards": [
                {
                    "front": "non-empty question or cue",
                    "back": "non-empty grounded answer",
                    "source_ids": ["SRC-1"],
                }
            ]
        }
        return _generation_prompt(
            instruction=(
                f"Generate exactly {count} unique learning cards. "
                "Every claim must be supported by one or more listed source_ids."
            ),
            schema=schema,
            sources=sources,
        )

    @staticmethod
    def _exercises_prompt(count: int, kind: str, sources: dict[str, dict]) -> str:
        if kind == "mixed":
            mcq_count = (count + 1) // 2
            short_count = count // 2
            distribution = (
                f"Generate {mcq_count} multiple_choice and {short_count} "
                "short_answer exercises."
            )
        else:
            distribution = f"Every exercise must have type {kind}."
        schema = {
            "exercises": [
                {
                    "type": "multiple_choice",
                    "prompt": "non-empty prompt",
                    "options": ["A", "B", "C", "D"],
                    "correct_index": 0,
                    "explanation": "non-empty explanation",
                    "source_ids": ["SRC-1"],
                },
                {
                    "type": "short_answer",
                    "prompt": "non-empty prompt",
                    "reference_answer": "non-empty reference answer",
                    "key_points": ["one to eight unique key points"],
                    "source_ids": ["SRC-1"],
                },
            ]
        }
        return _generation_prompt(
            instruction=(
                f"Generate exactly {count} unique exercises. {distribution} "
                "Multiple-choice exercises require exactly four unique options. "
                "Every exercise must cite one or more listed source_ids."
            ),
            schema=schema,
            sources=sources,
        )

    def _generate_json(self, prompt: str, wrapper: str) -> list[Any]:
        response = self._call_llm(prompt)
        try:
            return _parse_wrapper(response, wrapper)
        except _RepairableJSONError:
            repair_prompt = (
                "Repair the following response into strict JSON only. "
                f"The root must be exactly an object with one '{wrapper}' list. "
                "Do not change semantic field values or invent source IDs.\n\n"
                f"Response to repair:\n{response}"
            )
            repaired = self._call_llm(repair_prompt)
            try:
                return _parse_wrapper(repaired, wrapper)
            except _RepairableJSONError as exc:
                raise LearningGenerationError(
                    "LLM response is not valid JSON with the required root shape"
                ) from exc

    def _call_llm(self, prompt: str) -> str:
        if self.llm is None:
            raise LearningGenerationError("[LLM未配置]")
        generate = getattr(self.llm, "generate", None)
        chat = getattr(self.llm, "chat", None)
        method = generate if callable(generate) else chat if callable(chat) else None
        if method is None:
            raise LearningGenerationError("[LLM未配置]")
        try:
            response = method(prompt)
        except Exception as exc:
            raise LearningGenerationError("[LLM调用失败]") from exc
        if not isinstance(response, str):
            raise LearningGenerationError("LLM response must be a JSON string")
        if "[LLM未配置]" in response or "[LLM调用失败]" in response:
            raise LearningGenerationError(response)
        return response

    @staticmethod
    def _card(raw_item: Any, sources: dict[str, dict]) -> dict:
        item = _exact_object(raw_item, {"front", "back", "source_ids"}, "card")
        return {
            "front": _required_string(item["front"], "card front"),
            "back": _required_string(item["back"], "card back"),
            "sources": _resolved_sources(item["source_ids"], sources),
        }

    @staticmethod
    def _exercise(raw_item: Any, sources: dict[str, dict]) -> dict:
        if not isinstance(raw_item, dict):
            raise LearningGenerationError("exercise must be an object")
        exercise_type = raw_item.get("type")
        if exercise_type == "multiple_choice":
            item = _exact_object(
                raw_item,
                {
                    "type",
                    "prompt",
                    "options",
                    "correct_index",
                    "explanation",
                    "source_ids",
                },
                "multiple-choice exercise",
            )
            options = item["options"]
            if (
                not isinstance(options, list)
                or len(options) != 4
                or not all(isinstance(option, str) and option.strip() for option in options)
                or len({_normalized(option) for option in options}) != 4
            ):
                raise LearningGenerationError(
                    "multiple-choice options must be four unique non-empty strings"
                )
            correct_index = item["correct_index"]
            if not _is_int(correct_index) or not 0 <= correct_index <= 3:
                raise LearningGenerationError(
                    "correct_index must be an integer in 0..3"
                )
            return {
                "type": exercise_type,
                "prompt": _required_string(item["prompt"], "exercise prompt"),
                "options": [option.strip() for option in options],
                "correct_index": correct_index,
                "explanation": _required_string(
                    item["explanation"], "multiple-choice explanation"
                ),
                "sources": _resolved_sources(item["source_ids"], sources),
            }
        if exercise_type == "short_answer":
            item = _exact_object(
                raw_item,
                {
                    "type",
                    "prompt",
                    "reference_answer",
                    "key_points",
                    "source_ids",
                },
                "short-answer exercise",
            )
            key_points = item["key_points"]
            if (
                not isinstance(key_points, list)
                or not 1 <= len(key_points) <= 8
                or not all(isinstance(point, str) and point.strip() for point in key_points)
                or len({_normalized(point) for point in key_points}) != len(key_points)
            ):
                raise LearningGenerationError(
                    "key_points must be 1..8 unique non-empty strings"
                )
            return {
                "type": exercise_type,
                "prompt": _required_string(item["prompt"], "exercise prompt"),
                "reference_answer": _required_string(
                    item["reference_answer"], "reference_answer"
                ),
                "key_points": [point.strip() for point in key_points],
                "sources": _resolved_sources(item["source_ids"], sources),
            }
        raise LearningGenerationError(
            "exercise type must be multiple_choice or short_answer"
        )

    @staticmethod
    def _validate_exercise_distribution(
        drafts: list[dict], count: int, kind: str
    ) -> None:
        multiple_choice = sum(
            draft["type"] == "multiple_choice" for draft in drafts
        )
        short_answer = sum(draft["type"] == "short_answer" for draft in drafts)
        if kind == "mixed":
            if multiple_choice != (count + 1) // 2 or short_answer != count // 2:
                raise LearningGenerationError(
                    "mixed exercises must contain one more multiple-choice item when odd"
                )
        elif kind == "multiple_choice" and multiple_choice != count:
            raise LearningGenerationError("all exercises must be multiple_choice")
        elif kind == "short_answer" and short_answer != count:
            raise LearningGenerationError("all exercises must be short_answer")


def _generation_prompt(
    *, instruction: str, schema: dict[str, Any], sources: dict[str, dict]
) -> str:
    evidence = []
    for source_id, source in sources.items():
        evidence.append(
            {
                "source_id": source_id,
                "content": source["_content"],
                "page_number": source["page_number"],
                "source_position": source["source_position"],
            }
        )
    return (
        f"{instruction}\n"
        "Return strict JSON only, without Markdown fences or extra keys.\n"
        f"Required shape example:\n{json.dumps(schema, ensure_ascii=False)}\n"
        f"Grounding evidence:\n{json.dumps(evidence, ensure_ascii=False)}"
    )


def _parse_wrapper(response: str, wrapper: str) -> list[Any]:
    text = _strip_optional_fence(response)
    try:
        root = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _RepairableJSONError("LLM response has invalid JSON syntax") from exc
    if not isinstance(root, dict) or set(root) != {wrapper}:
        raise _RepairableJSONError("LLM response has the wrong root wrapper")
    items = root[wrapper]
    if not isinstance(items, list):
        raise _RepairableJSONError("LLM response wrapper must contain a list")
    return items


def _strip_optional_fence(response: str) -> str:
    text = response.strip()
    lines = text.splitlines()
    if len(lines) >= 2 and lines[0].strip().casefold() in {"```", "```json"}:
        if lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return text


def _exact_object(value: Any, keys: set[str], name: str) -> dict:
    if not isinstance(value, dict):
        raise LearningGenerationError(f"{name} must be an object")
    if set(value) != keys:
        raise LearningGenerationError(f"{name} must contain exactly {sorted(keys)}")
    return value


def _resolved_sources(source_ids: Any, sources: dict[str, dict]) -> list[dict]:
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or not all(isinstance(source_id, str) and source_id for source_id in source_ids)
        or len(set(source_ids)) != len(source_ids)
    ):
        raise LearningGenerationError(
            "source_ids must contain unique non-empty source IDs"
        )
    resolved = []
    for source_id in source_ids:
        source = sources.get(source_id)
        if source is None:
            raise LearningGenerationError(f"unknown source ID: {source_id}")
        public_source = {key: value for key, value in source.items() if key != "_content"}
        resolved.append(deepcopy(public_source))
    return resolved


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LearningGenerationError(f"{name} must be a non-empty string")
    return value.strip()


def _normalized(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().split())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _optional_int(value: Any) -> int | None:
    return value if _is_int(value) else None


def _is_pdf_source(metadata: dict) -> bool:
    source_type = str(metadata.get("source_type") or "").strip().casefold()
    suffix = str(metadata.get("file_suffix") or "").strip().casefold()
    return source_type == "pdf" or suffix == ".pdf"
