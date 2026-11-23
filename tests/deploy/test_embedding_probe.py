# tests/deploy/test_embedding_probe.py
import json
from pathlib import Path
import httpx
import pytest

from deploy.embedding_probe import main, read_values
from hello_agents.memory.rag.embedding_client import SiliconFlowEmbedding


def write_env(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=keep-me\nRAG_EMBEDDING_PROVIDER=simple\n"
                    "RAG_EMBEDDING_API_KEY=private-test-token\n", encoding="utf-8")
    return path


def test_configuration_check_is_offline_and_does_not_modify_file(tmp_path, capsys):
    path = write_env(tmp_path)
    original = path.read_bytes()
    def forbidden(settings):
        pytest.fail("offline check constructed remote client")
    assert main(["--env-file", str(path)], client_factory=forbidden) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "configuration_valid"
    assert data["activation"] == "unchanged"
    assert data["key_configured"] is True
    assert "private-test-token" not in json.dumps(data)
    assert path.read_bytes() == original


def test_probe_uses_public_text_only_and_no_qdrant(tmp_path, capsys):
    path = write_env(tmp_path)
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body["input"])
        return httpx.Response(200, json={"model": "BAAI/bge-m3", "data": [
            {"index": i, "embedding": [1.0] + [0.0] * 1023}
            for i in range(len(body["input"]))
        ]})
    def factory(settings):
        return SiliconFlowEmbedding(settings, transport=httpx.MockTransport(handler))
    assert main(["--env-file", str(path), "--probe"], client_factory=factory) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "probe_passed"
    assert data["vector_count"] == 3
    assert calls == [["知研公开连通性测试：水在标准大气压下约一百度沸腾。",
                      "Public connectivity test: plants use sunlight for photosynthesis."],
                     ["What do plants use sunlight for?"]]
    assert "private-test-token" not in json.dumps(data)


def test_probe_errors_are_safe(tmp_path, capsys):
    path = write_env(tmp_path)
    def factory(settings):
        return SiliconFlowEmbedding(settings, transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="private-test-token")))
    assert main(["--env-file", str(path), "--probe"], client_factory=factory) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "failed" and data["http_status"] == 401
    assert "private-test-token" not in json.dumps(data)


def test_no_interpolation_or_duplicate_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET", "private-test-token")
    path = tmp_path / ".env"
    path.write_text("RAG_EMBEDDING_API_KEY=${SECRET}\n", encoding="utf-8")
    assert read_values(path)["RAG_EMBEDDING_API_KEY"] == "${SECRET}"
    path.write_text("RAG_EMBEDDING_API_KEY=one\nRAG_EMBEDDING_API_KEY=two\n",
                    encoding="utf-8")
    from hello_agents.memory.rag.embedding_profile import EmbeddingFailure
    with pytest.raises(EmbeddingFailure, match="environment_file"):
        read_values(path)


def test_missing_key_and_file_fail_without_network(tmp_path, capsys):
    missing = tmp_path / "missing.env"
    assert main(["--env-file", str(missing)]) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "environment_file"
    path = tmp_path / ".env"
    path.write_text("LLM_API_KEY=keep-me\n", encoding="utf-8")
    assert main(["--env-file", str(path)]) == 1
    output = capsys.readouterr().out
    assert "keep-me" not in output
    assert json.loads(output)["code"] == "configuration"


def test_template_is_dormant_and_docker_copies_probe():
    root = Path(__file__).resolve().parents[2]
    template = (root / "deploy/.env.example").read_text(encoding="utf-8")
    assert "RAG_EMBEDDING_PROVIDER=simple" in template
    assert "RAG_EMBEDDING_MODEL=BAAI/bge-m3" in template
    assert "RAG_EMBEDDING_API_KEY=\n" in template
    assert "deploy/embedding_probe.py" in (root / "Dockerfile").read_text(encoding="utf-8")


@pytest.mark.parametrize("suffix", [
    "'RAG_EMBEDDING_API_KEY'=different-secret\n",
    "export RAG_EMBEDDING_API_KEY=different-secret\n",
    'RAG_EMBEDDING_MODEL="unterminated\n',
])
def test_ambiguous_or_malformed_file_fails_closed(tmp_path, capsys, suffix):
    path = write_env(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + suffix, encoding="utf-8")
    assert main(["--env-file", str(path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "environment_file"
    assert "different-secret" not in output


def test_unrelated_multiline_value_is_not_an_embedding_assignment(tmp_path, capsys):
    path = tmp_path / ".env"
    path.write_text('LLM_NOTE="line one\nRAG_EMBEDDING_MODEL=not-a-setting\n"\n'
                    'RAG_EMBEDDING_API_KEY=private-test-token\n'
                    'RAG_EMBEDDING_MODEL=BAAI/bge-m3\n', encoding="utf-8")
    assert main(["--env-file", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "configuration_valid"


def test_unresolved_key_does_not_inherit_process_secret(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    monkeypatch.setenv("SECRET", "must-not-be-used")
    path.write_text("RAG_EMBEDDING_API_KEY=${SECRET}\n", encoding="utf-8")
    assert main(["--env-file", str(path)]) == 1
    output = capsys.readouterr().out
    assert json.loads(output)["code"] == "configuration"
    assert "must-not-be-used" not in output


def test_billing_probe_is_terminal_and_actionable(tmp_path, capsys):
    path = write_env(tmp_path)
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(402, json={"code": 30001, "message": "private-test-token"})

    def factory(settings):
        return SiliconFlowEmbedding(settings, transport=httpx.MockTransport(handle))

    assert main(["--env-file", str(path), "--probe"], client_factory=factory) == 1
    data = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert data["reason"] == "provider_billing" and data["retryable"] is False
    assert data["action"] == "check_account_balance_or_key"
    assert "private-test-token" not in json.dumps(data)
