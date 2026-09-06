"""Offline acceptance through the real assistant, generation and persistence layers."""
import json
from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.history import HistoryRepository
from app.learning import LearningService
from app.learning_generation import LearningGenerationService
from app.learning_repository import LearningRepository
from app.storage import UserStorage
from assistants.pdf_learning_assistant import PDFLearningAssistant


class EvidenceRAG:
    def __init__(self, document_id):
        self.document_id = document_id
        self.calls = []

    def execute_result(self, action, **kwargs):
        assert action == "search"
        assert kwargs["document_id"] == self.document_id
        self.calls.append(kwargs)
        return SimpleNamespace(success=True, data={"results": [{
            "chunk_id": self.document_id + "-chunk", "score": 0.9,
            "content": "Spaced review improves retention.",
            "metadata": {"document_id": self.document_id,
                         "file_name": "notes.pdf", "source_type": "pdf",
                         "page_number": 3},
        }]})

    def execute(self, action, **kwargs):
        assert action in {"delete_document", "clear"}
        return "deleted"


class ScriptedLLM:
    def __init__(self):
        self.responses = [
            {"cards": [{"front": f"Concept {i}", "back": "Review regularly",
                        "source_ids": ["SRC-1"]} for i in range(3)]},
            {"exercises": [
                {"type": "multiple_choice", "prompt": f"Question {i}",
                 "options": ["Review", "Ignore", "Skip", "Forget"],
                 "correct_index": 0, "explanation": "Review aids retention",
                 "source_ids": ["SRC-1"]} for i in range(2)
            ] + [{"type": "short_answer", "prompt": "Explain retention",
                  "reference_answer": "Use spaced review", "key_points": ["Spacing"],
                  "source_ids": ["SRC-1"]}]},
        ]

    def generate(self, prompt):
        assert "SRC-1" in prompt
        return json.dumps(self.responses.pop(0))


def make_assistant(storage, user_id, document_id, now):
    paths = storage.ensure_user_dirs(user_id)
    history = HistoryRepository(paths.history)
    source = paths.documents / "notes.pdf"
    source.write_bytes(b"test fixture")
    history.update(lambda data: data["documents"].append({
        "document_id": document_id, "document_name": "notes.pdf",
        "document_path": str(source),
    }))
    lock = RLock()
    repository = LearningRepository(paths.learning, lock)
    service = LearningService(repository, now=lambda: now, today=lambda: now.date())
    rag = EvidenceRAG(document_id)
    runtime = SimpleNamespace(paths=paths, lock=lock, history=history, coordinator=None,
        reports=None, memory_tool=SimpleNamespace(), rag_tool=rag,
        import_task_service=None, learning_repository=repository, learning_service=service,
        learning_generation_service=LearningGenerationService(rag, ScriptedLLM()))
    assistant = PDFLearningAssistant(user_id=user_id, runtime=runtime)
    assistant.current_document_id = document_id
    assistant.current_document = str(source)
    return assistant


def test_two_users_generate_review_restart_and_delete_independently(tmp_path):
    now = datetime(2026, 9, 5, 9, tzinfo=timezone.utc)
    storage = UserStorage(tmp_path / "data")
    alice = make_assistant(storage, str(uuid4()), "alice-doc", now)
    bob = make_assistant(storage, str(uuid4()), "bob-doc", now)
    try:
        for assistant in (alice, bob):
            plan = assistant.create_learning_plan("Daily study", now.date().isoformat(), 30)
            assistant.set_learning_task_completed(plan["id"], plan["tasks"][0]["id"], True)
            cards = assistant.generate_learning_cards(3)
            exercises = assistant.generate_learning_exercises(3, "mixed")
            assert len(assistant.get_learning_dashboard()["due_cards"]) == 3
            assert assistant.get_learning_dashboard()["completion_rate"] == 1
            reviewed = assistant.review_learning_card(cards[0]["id"], "mastered")
            assert datetime.fromisoformat(reviewed["schedule"]["due_at"]) > now
            mcq = next(item for item in exercises if item["type"] == "multiple_choice")
            short = next(item for item in exercises if item["type"] == "short_answer")
            assert assistant.answer_learning_multiple_choice(mcq["id"], 0)["correct"]
            assistant.review_learning_short_answer(short["id"], "fuzzy", "Spacing helps")
            assert len(assistant.get_learning_dashboard()["due_cards"]) == 2
            restored = LearningRepository(assistant.learning_repository.path, RLock()).load()
            assert len(restored["review_logs"]) == 3
            for item in restored["cards"] + restored["exercises"]:
                assert item["sources"][0]["document_id"] == assistant.current_document_id
                assert item["sources"][0]["chunk_id"] == assistant.current_document_id + "-chunk"
                assert item["sources"][0]["page_number"] == 3

        bob_bytes = bob.learning_repository.path.read_bytes()
        with pytest.raises(ValueError):
            alice.review_learning_card(bob.get_learning_cards()[0]["id"], "mastered")
        result = alice.delete_current_document()
        assert "partial failure" not in result
        assert all(not alice.learning_repository.load()[key]
                   for key in ("plans", "cards", "exercises", "review_logs"))
        assert bob.learning_repository.path.read_bytes() == bob_bytes
        assert len(bob.get_learning_cards()) == 3
    finally:
        alice.close()
        bob.close()
