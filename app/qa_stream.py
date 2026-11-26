from __future__ import annotations

import asyncio
from collections import OrderedDict
from threading import Lock
from typing import Literal


QaStage = Literal["retrieving", "reranking", "verifying", "generating"]
QaStreamEvent = tuple[str, dict]
_StreamListener = tuple[asyncio.AbstractEventLoop, asyncio.Event]


class QaLiveAnswer:
    """单进程内存中的有界实时回答回放与跨线程异步唤醒器。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._events: list[QaStreamEvent] = []
        self._token_chars = 0
        self._overflowed = False
        self._closed = False
        self._listeners: set[_StreamListener] = set()

    def _notify_locked(self) -> None:
        """在持锁状态下唤醒所有正在等待新事件的 SSE 协程。"""
        stale: list[_StreamListener] = []
        for loop, event in self._listeners:
            if loop.is_closed():
                stale.append((loop, event))
                continue
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                stale.append((loop, event))
        for item in stale:
            self._listeners.discard(item)

    def subscribe(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        """注册异步等待事件；若已关闭则立即置位避免遗漏终态。"""
        with self._lock:
            self._listeners.add((loop, event))
            if self._closed or self._overflowed:
                event.set()

    def unsubscribe(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        """反注册等待事件，防止断连协程残留引用。"""
        with self._lock:
            self._listeners.discard((loop, event))

    def stage(self, stage: QaStage, retrieved_count: int | None = None) -> None:
        data: dict[str, object] = {"stage": stage}
        if retrieved_count is not None:
            data["retrieved_count"] = retrieved_count
        with self._lock:
            if not self._overflowed and self._events[-1:] != [("stage", data)]:
                self._events.append(("stage", data))
                self._notify_locked()

    def token(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._token_chars += len(delta)
            if self._token_chars > 128_000 or len(self._events) >= 8192:
                self._overflowed = True
                self._notify_locked()
                return
            self._events.append(("token", {"delta": delta}))
            self._notify_locked()

    def close(self) -> None:
        """标记生产结束并立刻唤醒所有监听者去读取 SQLite 最终持久化状态。"""
        with self._lock:
            self._closed = True
            self._notify_locked()

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
            live = self._active.pop(message_id, None)
            if live is None:
                return
            self._recent[message_id] = live
            if len(self._recent) > 32:
                self._recent.popitem(last=False)
        # 在释放注册表锁后通知监听者，避免锁嵌套
        live.close()
