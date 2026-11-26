from __future__ import annotations

from collections import OrderedDict
from threading import Lock
from typing import Literal


QaStage = Literal["retrieving", "reranking", "verifying", "generating"]
QaStreamEvent = tuple[str, dict]


class QaLiveAnswer:
    """Bounded replay for one answer in the current server process."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[QaStreamEvent] = []
        self._token_chars = 0
        self._overflowed = False

    def stage(self, stage: QaStage, retrieved_count: int | None = None) -> None:
        data = {"stage": stage}
        if retrieved_count is not None:
            data["retrieved_count"] = retrieved_count
        with self._lock:
            if not self._overflowed and self._events[-1:] != [("stage", data)]:
                self._events.append(("stage", data))

    def token(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._token_chars += len(delta)
            if self._token_chars > 128_000 or len(self._events) >= 8192:
                self._overflowed = True
            if not self._overflowed:
                self._events.append(("token", {"delta": delta}))

    def read(self, cursor: int) -> tuple[tuple[QaStreamEvent, ...], bool]:
        with self._lock:
            return tuple(self._events[cursor:]), self._overflowed


class QaLiveAnswers:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[str, QaLiveAnswer] = {}
        self._recent: OrderedDict[str, QaLiveAnswer] = OrderedDict()

    def start(self, message_id: str) -> QaLiveAnswer:
        live = QaLiveAnswer()
        with self._lock:
            self._active[message_id] = live
        return live

    def get(self, message_id: str) -> QaLiveAnswer | None:
        with self._lock:
            return self._active.get(message_id) or self._recent.get(message_id)

    def finish(self, message_id: str) -> None:
        with self._lock:
            self._recent[message_id] = self._active.pop(message_id)
            if len(self._recent) > 32:
                self._recent.popitem(last=False)
