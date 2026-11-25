"""Isolated real-server entry point for QA Playwright acceptance only."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import uvicorn

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from api.app import create_application
from app.bootstrap import ApplicationServices
from app.qa_answer_engine import QaAnswerRequest, QaAnswerResult, QaEngineError
from app.qa_models import QaSourceDraft


class DeterministicQaAnswerEngine:
    """Deterministic adapter reachable only through this test executable."""

    def __init__(self) -> None:
        self._failures: set[str] = set()

    def answer(
        self,
        runtime: object,
        request: QaAnswerRequest,
        *,
        progress_callback: object | None = None,
        cancel_event: object | None = None,
    ) -> QaAnswerResult:
        if request.mode == "summary" and (
            "[incomplete-once]" in request.question and request.question not in self._failures
        ):
            self._failures.add(request.question)
            raise QaEngineError("QA_SUMMARY_INCOMPLETE", retryable=False)

        if "[fail-once]" in request.question and request.question not in self._failures:
            self._failures.add(request.question)
            raise QaEngineError("QA_ENGINE_UNAVAILABLE", retryable=True)

        if callable(progress_callback):
            if request.mode == "summary":
                for _ in range(20):
                    progress_callback("starting", 0, 5, "safe")
                    time.sleep(0.08)
            for completed in range(1, 5):
                progress_callback("summarizing", completed, 5, "safe")
                time.sleep(0.08)

        document_id = request.document_ids[0]
        records = runtime.history.load().get("documents", [])
        record = next(
            (item for item in records if item.get("document_id") == document_id),
            {},
        )
        document_name = Path(str(record.get("document_name") or "测试文档.md")).name
        source = QaSourceDraft(
            citation_id="E2E-S1",
            document_id=document_id,
            document_name=document_name,
            page_number=1,
            section="测试证据",
            excerpt="这是由隔离测试适配器生成的可验证证据片段。",
            reference="[E2E-S1]",
            source_type="rag",
        )
        answer = (
            "这是持久化学习摘要，概括了当前固定文档范围。"
            if request.mode == "summary"
            else "这是确定性的可信回答，可通过右侧引用核对来源。"
        )
        return QaAnswerResult(answer, (source,), request.mode)


def build_app():
    data_root = os.environ.get("PDF_ASSISTANT_DATA_DIR")
    if not data_root:
        raise RuntimeError("PDF_ASSISTANT_DATA_DIR is required for QA E2E")
    services = ApplicationServices.create(
        Path(data_root), qa_answer_engine=DeterministicQaAnswerEngine()
    )
    return create_application(services)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    uvicorn.run(build_app(), host=arguments.host, port=arguments.port)
