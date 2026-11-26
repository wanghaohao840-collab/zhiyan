import unittest
from types import SimpleNamespace

from hello_agents.core.llm import HelloAgentsLLM


class FakeTransportError(Exception):
    pass


class FakeChatCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def completion(text):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
            )
        ]
    )


def stream_chunks(*texts):
    def iterator():
        for text in texts:
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=text),
                    )
                ]
            )

    return iterator()


def broken_stream():
    yield SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="partial "),
            )
        ]
    )
    raise FakeTransportError("error decoding response body")


class HelloAgentsLLMTransportTests(unittest.TestCase):
    def make_llm(self, outcomes, **kwargs):
        kwargs.setdefault("retry_backoff", 0)
        llm = HelloAgentsLLM(api_key=None, **kwargs)
        llm.client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeChatCompletions(outcomes))
        )
        return llm

    def test_chat_retries_transient_transport_decode_errors(self):
        llm = self.make_llm(
            [
                FakeTransportError("stream disconnected before completion"),
                FakeTransportError("error decoding response body"),
                completion("ok"),
            ],
            max_retries=2,
        )

        self.assertEqual(llm.chat("hello"), "ok")
        self.assertEqual(len(llm.client.chat.completions.calls), 3)

    def test_invoke_accepts_openai_style_messages(self):
        messages = [{"role": "user", "content": "hello"}]
        llm = self.make_llm([completion("ok")])

        self.assertEqual(llm.invoke(messages), "ok")
        sent_messages = llm.client.chat.completions.calls[0]["messages"]
        self.assertEqual(sent_messages, messages)

    def test_stream_invoke_falls_back_to_non_stream_after_disconnect(self):
        llm = self.make_llm(
            [
                broken_stream(),
                completion("complete answer"),
            ],
            max_retries=1,
        )

        self.assertEqual("".join(llm.stream_invoke("hello")), "partial complete answer")
        self.assertTrue(llm.client.chat.completions.calls[0]["stream"])
        self.assertNotIn("stream", llm.client.chat.completions.calls[1])

    def test_strict_stream_does_not_replay_or_expose_transport_errors_as_text(self):
        llm = self.make_llm([broken_stream(), completion("duplicate")])
        stream = llm.stream_invoke("hello", strict=True)
        self.assertEqual(next(stream), "partial ")
        with self.assertRaises(FakeTransportError):
            next(stream)
        self.assertEqual(len(llm.client.chat.completions.calls), 1)

    def test_strict_stream_requires_a_successful_finish_reason(self):
        valid = [SimpleNamespace(choices=[SimpleNamespace(
            delta=SimpleNamespace(content="complete"), finish_reason="stop",
        )])]
        llm = self.make_llm([iter(valid), stream_chunks("no finish frame")])
        self.assertEqual(list(llm.stream_invoke("hello", strict=True)), ["complete"])
        with self.assertRaisesRegex(RuntimeError, "ended before a complete answer"):
            list(llm.stream_invoke("hello", strict=True))

    def test_strict_stream_handles_openai_compatible_empty_and_length_finish_reasons(self):
        empty_mid_then_stop = [
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="第一段"), finish_reason="",
            )]),
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="第二段"), finish_reason="stop",
            )]),
        ]
        length_truncated = [
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="达到上限的完整正文"), finish_reason="length",
            )]),
        ]
        filtered = [
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="被截断"), finish_reason="content_filter",
            )]),
        ]
        llm = self.make_llm([iter(empty_mid_then_stop), iter(length_truncated), iter(filtered)])
        self.assertEqual(list(llm.stream_invoke("hello", strict=True)), ["第一段", "第二段"])
        self.assertEqual(list(llm.stream_invoke("hello", strict=True)), ["达到上限的完整正文"])
        with self.assertRaisesRegex(RuntimeError, "content_filter"):
            list(llm.stream_invoke("hello", strict=True))


if __name__ == "__main__":
    unittest.main()
