"""Isolated real-server entry point for Notes Playwright acceptance only."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from api.app import create_application
from app.bootstrap import ApplicationServices
from app.note_models import Note
from app.note_projection import DefaultNoteMemoryProjection
from app.qa_answer_engine import QaAnswerRequest, QaAnswerResult
from app.qa_models import QaSourceDraft
from app.runtime import UserRuntime


class DeterministicQaAnswerEngine:
    """Deterministic QA adapter reachable only through this executable."""

    def answer_stream(self, runtime, request, *, stage_callback, token_callback):
        stage_callback("retrieving", 1)
        answer = self.answer(runtime, request)
        stage_callback("generating", None)
        token_callback(answer.answer)
        return answer

    def answer(
        self,
        runtime: object,
        request: QaAnswerRequest,
        *,
        progress_callback: object | None = None,
        cancel_event: object | None = None,
    ) -> QaAnswerResult:
        if callable(progress_callback):
            for completed in range(1, 3):
                progress_callback("summarizing", completed, 2, "safe")
                time.sleep(0.04)
        document_id = request.document_ids[0]
        records = runtime.history.load().get("documents", [])
        record = next(
            (item for item in records if item.get("document_id") == document_id),
            {},
        )
        document_name = Path(str(record.get("document_name") or "Notes证据.md")).name
        source = QaSourceDraft(
            citation_id="NOTES-E2E-S1",
            document_id=document_id,
            document_name=document_name,
            page_number=1,
            section="Notes验收证据",
            excerpt="这是 Notes 垂直切片的确定性来源片段。",
            reference="[NOTES-E2E-S1]",
            source_type="rag",
        )
        return QaAnswerResult(
            "这是用于 Notes 保存来源验收的确定性回答。",
            (source,),
            request.mode,
        )


class FailMarkedProjection:
    """Fail marked Notes through the durable retry limit, then converge."""

    def __init__(self) -> None:
        self._delegate = DefaultNoteMemoryProjection()
        self._attempts: dict[str, int] = defaultdict(int)

    def upsert(self, runtime: UserRuntime, note: Note) -> None:
        if "[projection-fail]" in note.body_markdown and self._attempts[note.id] < 3:
            self._attempts[note.id] += 1
            raise RuntimeError("intentional Notes E2E projection failure")
        self._delegate.upsert(runtime, note)

    def remove(self, runtime: UserRuntime, note_id: str) -> bool:
        return self._delegate.remove(runtime, note_id)


def build_app():
    data_root = os.environ.get("PDF_ASSISTANT_DATA_DIR")
    if not data_root:
        raise RuntimeError("PDF_ASSISTANT_DATA_DIR is required for Notes E2E")
    services = ApplicationServices.create(
        Path(data_root), qa_answer_engine=DeterministicQaAnswerEngine()
    )
    services.note_projection_repository.retry_delay_seconds = 0
    services.note_projection_worker.projection = FailMarkedProjection()
    return create_application(services)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(build_app(), host=arguments.host, port=arguments.port)
