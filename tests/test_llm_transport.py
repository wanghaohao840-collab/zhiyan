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

    def test_metadata_is_optional_and_does_not_change_chat_text(self):
        incomplete = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content="partial", reasoning_content="private reasoning"),
        )])
        llm = self.make_llm([incomplete, incomplete, completion("compatible")])
        self.assertEqual(llm.chat("ordinary question"), "partial")
        result = llm.generate_with_metadata("summary", max_tokens=640, strict=True)
        self.assertEqual((result.text, result.finish_reason), ("partial", "length"))
        self.assertNotIn("private reasoning", repr(result))
        self.assertIsNone(llm.generate_with_metadata("summary").finish_reason)
        calls = llm.client.chat.completions.calls
        self.assertEqual([call["max_tokens"] for call in calls], [1024, 640, 1024])
        self.assertTrue(all("strict" not in call for call in calls))

    def test_metadata_transport_errors_preserve_retry_mode(self):
        for strict, count in ((True, 1), (False, 3)):
            with self.subTest(strict=strict):
                llm = self.make_llm(
                    [FakeTransportError("transport error")] * 3, max_retries=2
                )
                with self.assertRaises(FakeTransportError):
                    llm.generate_with_metadata("summary", strict=strict)
                self.assertEqual(len(llm.client.chat.completions.calls), count)

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


if __name__ == "__main__":
    unittest.main()
