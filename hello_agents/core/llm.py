# hello_agents/core/llm.py

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


@dataclass(frozen=True)
class LLMCompletionResult:
    text: str
    finish_reason: str | None = None


class HelloAgentsLLM:
    """Unified LLM wrapper for OpenAI-compatible chat APIs."""

    def __init__(
        self,
        model: Optional[str] = None,
        model_id: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        max_retries: Optional[int] = None,
        retry_backoff: Optional[float] = None,
        context_window_tokens: Optional[int] = None,
        tokenizer: Any = None,
        **kwargs,
    ):
        self.model = (
            model
            or model_id
            or os.getenv("LLM_MODEL_ID")
            or os.getenv("OPENAI_MODEL")
            or "deepseek-chat"
        )

        self.api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or os.getenv("DEEPSEEK_API_KEY")
        )

        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.deepseek.com/v1"
        )

        self.timeout = timeout
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.getenv("LLM_MAX_RETRIES", "2"))
        )
        self.retry_backoff = (
            retry_backoff
            if retry_backoff is not None
            else float(os.getenv("LLM_RETRY_BACKOFF", "0.5"))
        )
        self.context_window_tokens = (
            context_window_tokens
            if context_window_tokens is not None
            else int(os.getenv("LLM_CONTEXT_WINDOW_TOKENS", "8192"))
        )
        self.tokenizer = tokenizer
        self.client = None

        if OpenAI is not None and self.api_key:
            self.client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,
            )

    def estimate_tokens(self, text: str) -> int:
        """Estimate tokens with an injected matching tokenizer when available."""

        value = str(text or "")
        encode = getattr(self.tokenizer, "encode", None)
        if callable(encode):
            return len(encode(value))
        return len(value)

    def _normalize_messages(
        self,
        prompt: Any,
        system_prompt: Optional[str],
    ) -> List[Dict[str, str]]:
        if isinstance(prompt, list):
            messages = []
            for message in prompt:
                if hasattr(message, "to_dict"):
                    message = message.to_dict()
                messages.append(
                    {
                        "role": str(message["role"]),
                        "content": str(message["content"]),
                    }
                )
            return messages

        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": str(prompt)})
        return messages

    def _is_retriable_error(self, error: Exception) -> bool:
        error_text = f"{error.__class__.__name__}: {error}".lower()
        retriable_markers = (
            "stream disconnected before completion",
            "transport error",
            "network error",
            "error decoding response body",
            "connection",
            "connecterror",
            "readerror",
            "remoteprotocolerror",
            "timeout",
            "timed out",
            "api connection",
            "api timeout",
            "temporarily unavailable",
            "rate limit",
            "server error",
            "500",
            "502",
            "503",
            "504",
        )
        non_retriable_markers = (
            "authentication",
            "permission",
            "invalid_request",
            "badrequest",
            "401",
            "403",
            "404",
        )
        return (
            any(marker in error_text for marker in retriable_markers)
            and not any(marker in error_text for marker in non_retriable_markers)
        )

    def _sleep_before_retry(self, attempt: int) -> None:
        delay = self.retry_backoff * (2 ** max(attempt - 1, 0))
        if delay > 0:
            time.sleep(delay)

    def _completion_text(self, response: Any) -> str:
        return response.choices[0].message.content or ""

    def _stream_chunk_text(self, chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            return ""
        delta = getattr(chunk.choices[0], "delta", None)
        return getattr(delta, "content", None) or ""

    def _create_completion(self, messages: List[Dict[str, str]], **kwargs) -> Any:
        model = kwargs.pop("model", None) or self.model
        return self.client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs,
        )

    def chat(
        self,
        prompt: Any,
        system_prompt: str = "你是一个有帮助的AI助手。",
        temperature: float = 0.2,
        max_tokens: int = 1024,
        *,
        strict: bool = False,
        **kwargs,
    ) -> str:
        return self._chat_with_metadata(
            prompt, system_prompt, temperature, max_tokens,
            strict=strict, return_errors=True, **kwargs,
        ).text

    def _chat_with_metadata(
        self,
        prompt: Any,
        system_prompt: str = "你是一个有帮助的AI助手。",
        temperature: float = 0.2,
        max_tokens: int = 1024,
        *,
        strict: bool = False,
        return_errors: bool = False,
        **kwargs,
    ) -> LLMCompletionResult:
        """Run a chat completion. Strict calls surface failures without retrying."""

        if self.client is None:
            if strict or not return_errors:
                raise RuntimeError("LLM client is not configured")
            return LLMCompletionResult(
                "[LLM未配置] 当前没有可用的大模型客户端。"
                "请检查 .env 中的 LLM_API_KEY、LLM_BASE_URL、LLM_MODEL_ID。"
            )

        messages = self._normalize_messages(prompt, system_prompt)
        request_kwargs = {
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        request_kwargs.update(kwargs)

        attempts = 1 if strict else self.max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._create_completion(messages, **request_kwargs)
                reason = getattr(response.choices[0], "finish_reason", None)
                return LLMCompletionResult(
                    self._completion_text(response),
                    reason.strip().lower() if isinstance(reason, str) else None,
                )
            except Exception as error:
                if strict:
                    raise
                status = getattr(error, "status_code", None)
                rejected_model_request = bool(request_kwargs.get("model")) and status in (
                    400, 401, 403, 404, 422
                )
                if (
                    attempt + 1 >= attempts
                    or rejected_model_request
                    or not self._is_retriable_error(error)
                ):
                    if not return_errors:
                        raise
                    return LLMCompletionResult(f"[LLM调用失败] {error}")
                self._sleep_before_retry(attempt + 1)

        return LLMCompletionResult("[LLM调用失败] 未知错误")

    def stream_invoke(
        self,
        prompt: Any,
        system_prompt: str = "你是一个有帮助的AI助手。",
        temperature: float = 0.2,
        max_tokens: int = 1024,
        **kwargs,
    ) -> Iterable[str]:
        """Stream completion chunks; recover from transient transport errors."""

        if self.client is None:
            yield self.chat(prompt, system_prompt=system_prompt, **kwargs)
            return

        messages = self._normalize_messages(prompt, system_prompt)
        request_kwargs = {
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        request_kwargs.update(kwargs)

        for attempt in range(self.max_retries + 1):
            emitted_any = False
            try:
                stream = self._create_completion(messages, **request_kwargs)
                for chunk in stream:
                    text = self._stream_chunk_text(chunk)
                    if text:
                        emitted_any = True
                        yield text
                return
            except Exception as error:
                if not self._is_retriable_error(error):
                    yield f"[LLM流式调用失败] {error}"
                    return
                if emitted_any:
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs.pop("stream", None)
                    yield self.chat(
                        messages,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **fallback_kwargs,
                    )
                    return
                if attempt >= self.max_retries:
                    fallback_kwargs = dict(kwargs)
                    fallback_kwargs.pop("stream", None)
                    yield self.chat(
                        messages,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        **fallback_kwargs,
                    )
                    return
                self._sleep_before_retry(attempt + 1)

    def generate(self, prompt: Any, **kwargs) -> str:
        """Compatibility wrapper for generate calls."""

        return self.chat(prompt, **kwargs)

    def generate_with_metadata(self, prompt: Any, **kwargs) -> LLMCompletionResult:
        """Generate text with its finish reason, keeping transport errors typed."""
        return self._chat_with_metadata(prompt, **kwargs)

    def think(self, prompt: Any, **kwargs) -> str:
        """Compatibility wrapper for think calls."""

        return self.chat(prompt, **kwargs)

    def invoke(self, prompt: Any, **kwargs) -> str:
        """Compatibility wrapper for invoke calls."""

        return self.chat(prompt, **kwargs)

    def __call__(self, prompt: Any, **kwargs) -> str:
        """Compatibility wrapper for llm(prompt) calls."""

        return self.chat(prompt, **kwargs)
