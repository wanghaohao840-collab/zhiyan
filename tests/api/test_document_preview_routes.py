from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from api.app import create_api_app
from app.database import connect, initialize_database
from app.document_library import DocumentLibraryService
from app.qa_deletion import QaDeletionRepository, QaDeletionService
from app.session import InvalidCsrfTokenError, InvalidSessionError
from app.storage import UserStorage


DOCUMENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONTENT = f"/api/v1/documents/{DOCUMENT}/content"
PREVIEW = f"/api/v1/documents/{DOCUMENT}/chunks/current"


class Sessions(dict):
    def get_session(self, token):
        if token not in self:
            raise InvalidSessionError()
        return self[token]

    def validate_csrf(self, token, csrf):
        session = self.get_session(token)
        if session.csrf_token != csrf:
            raise InvalidCsrfTokenError()
        return session


def chunk(chunk_id, content, **metadata):
    return {
        "id": chunk_id,
        "content": content,
        "metadata": {"document_id": DOCUMENT, "page_number": 2, "section": "二", **metadata},
    }


@pytest.fixture
def preview_host(tmp_path):
    storage = UserStorage(tmp_path / "data")
    path = storage.ensure_user_dirs("owner").documents / "original.pdf"
    path.write_bytes(b"%PDF-123456789")
    record = {
        "document_id": DOCUMENT,
        "document_name": "论文.pdf",
        "file_suffix": ".pdf",
        "document_path": str(path),
    }
    backend = Mock(return_value=(
        chunk("previous", "上一段", page_number=1),
        chunk("current", "目标", preview_before="甲😀", preview_after="。乙"),
        chunk("next", "下一段"),
    ))
    sessions = Sessions({
        token: SimpleNamespace(
            user_id=token,
            csrf_token=f"{token}-csrf",
            runtime=SimpleNamespace(
                lock=RLock(),
                # Even a malformed history record pointing to another user's
                # source directory must fail the ownership preflight.
                history=SimpleNamespace(load=lambda: {"documents": [record]}),
                rag_tool=SimpleNamespace(get_document_preview_chunks=backend),
            ),
        )
        for token in ("owner", "other")
    })
    db_path = tmp_path / "preview.sqlite3"
    initialize_database(db_path)
    with connect(db_path) as conn:
        conn.execute("insert into users values ('owner','owner','owner','hash','active','now','now')")
    deletion_repository = QaDeletionRepository(db_path)
    library = DocumentLibraryService(
        sessions, storage, SimpleNamespace(has_active_task_for_document=lambda *_: False),
        deletion_repository=deletion_repository,
    )
    library.deletion_service = QaDeletionService(
        sessions, library, None, deletion_repository, SimpleNamespace(notify=lambda: None),
    )
    host = SimpleNamespace(
        start=lambda: None, stop=lambda: None, session_registry=sessions, document_library=library,
    )
    with TestClient(create_api_app(host)) as client:
        yield client, backend, path, deletion_repository, library


def test_document_range_preview_checks_auth_and_preserves_source_bytes(preview_host):
    client, _, path, _, _ = preview_host
    assert client.get(CONTENT).status_code == 401

    client.cookies.set("zhiyan_session", "other")
    assert client.get(CONTENT, headers={"Range": "bytes=0-3"}).status_code == 404

    client.cookies.set("zhiyan_session", "owner")
    entire = client.get(CONTENT)
    assert entire.status_code == 200
    assert entire.content == path.read_bytes()
    assert entire.headers["content-type"] == "application/pdf"
    assert entire.headers["accept-ranges"] == "bytes"
    assert entire.headers["content-disposition"].startswith("inline; filename*=UTF-8''")
    assert entire.headers["cache-control"] == "private, no-store"
    assert entire.headers["x-content-type-options"] == "nosniff"
    for value, expected, content_range in (
        ("bytes=0-3", b"%PDF", "bytes 0-3/14"),
        ("bytes=10-", b"6789", "bytes 10-13/14"),
        ("bytes=-2", b"89", "bytes 12-13/14"),
        ("bytes=12-99", b"89", "bytes 12-13/14"),
    ):
        result = client.get(CONTENT, headers={"Range": value})
        assert result.status_code == 206
        assert result.headers["content-range"] == content_range
        assert result.headers["content-length"] == str(len(expected))
        assert result.content == expected

    for value in ("bytes=30-", "bytes=-0", "bytes=7-6", "bytes=0-0,2-2", "bytes=a-b"):
        result = client.get(CONTENT, headers={"Range": value})
        assert result.status_code == 416
        assert result.headers["content-range"] == "bytes */14"


def test_chunk_preview_projects_neighbors_and_legacy_offsets(preview_host):
    client, backend, _, _, _ = preview_host
    assert client.get(PREVIEW).status_code == 401
    client.cookies.set("zhiyan_session", "other")
    assert client.get(PREVIEW).status_code == 404
    backend.assert_not_called()

    client.cookies.set("zhiyan_session", "owner")
    response = client.get(PREVIEW)
    assert response.status_code == 200
    body = response.json()
    assert body["document_id"] == DOCUMENT
    assert body["page_number"] == 2 and body["section"] == "二"
    assert body["context_text"] == "甲😀目标。乙"
    assert body["highlight_start"] == 2
    assert body["context_text"][body["highlight_start"]:body["highlight_end"]] == "目标"
    assert body["previous_chunk"]["chunk_id"] == "previous"
    assert body["next_chunk"]["chunk_id"] == "next"
    assert client.get(PREVIEW.replace("current", "unknown")).status_code == 404

    backend.return_value = (None, chunk("current", "旧摘录"), None)
    fallback = client.get(PREVIEW).json()
    assert fallback["context_text"] == "旧摘录"
    assert fallback["highlight_start"] is fallback["highlight_end"] is None


def test_legacy_document_id_can_preview_and_honors_the_fence(preview_host):
    client, backend, _, deletion_repository, library = preview_host
    legacy_id = "7a19b4a672d1b2c3"
    history = library.session_registry.get_session("owner").runtime.history
    record = history.load()["documents"][0]
    history.load = lambda: {"documents": [{**record, "document_id": legacy_id}]}
    backend.return_value = (None, chunk("current", "旧摘录", document_id=legacy_id), None)
    client.cookies.set("zhiyan_session", "owner")
    preview_url = PREVIEW.replace(DOCUMENT, legacy_id)
    content_url = CONTENT.replace(DOCUMENT, legacy_id)

    response = client.get(preview_url)
    assert response.status_code == 200
    assert response.json()["highlight_start"] is None
    assert client.get(content_url).status_code == 200
    deletion_repository.create_document_deletion("owner", legacy_id)
    assert client.get(preview_url).status_code == 404
    assert client.get(content_url).status_code == 404


def test_legacy_document_id_can_also_be_deleted_symmetrically(preview_host):
    client, _, _, _, library = preview_host
    legacy_id = "7a19b4a672d1b2c3"
    history = library.session_registry.get_session("owner").runtime.history
    record = history.load()["documents"][0]
    history.load = lambda: {"documents": [{**record, "document_id": legacy_id}]}
    client.cookies.set("zhiyan_session", "owner")

    deleted = client.delete(
        f"/api/v1/documents/{legacy_id}",
        headers={"X-CSRF-Token": "owner-csrf"},
    )
    assert deleted.status_code == 202


def test_text_markdown_content_declares_utf8_and_docx_returns_415_only_for_owner(preview_host):
    client, _, path, _, library = preview_host
    md_path = path.with_name("中文笔记.md")
    md_path.write_bytes("# 知研中文标题\n正文内容".encode("utf-8"))
    docx_path = path.with_name("报告.docx")
    docx_path.write_bytes(b"PK\x03\x04fake-docx")
    md_id = "11111111-1111-4111-8111-111111111111"
    docx_id = "22222222-2222-4222-8222-222222222222"
    records = [
        {
            "document_id": md_id,
            "document_name": "中文笔记.md",
            "file_suffix": ".md",
            "document_path": str(md_path),
        },
        {
            "document_id": docx_id,
            "document_name": "报告.docx",
            "file_suffix": ".docx",
            "document_path": str(docx_path),
        },
    ]
    for token in ("owner", "other"):
        library.session_registry.get_session(token).runtime.history.load = lambda r=records: {"documents": r}

    # 跨用户访问他人 DOCX 必须继续返回 404，不泄露文档存在与格式
    client.cookies.set("zhiyan_session", "other")
    assert client.get(f"/api/v1/documents/{docx_id}/content").status_code == 404

    client.cookies.set("zhiyan_session", "owner")
    md_resp = client.get(f"/api/v1/documents/{md_id}/content")
    assert md_resp.status_code == 200
    assert md_resp.headers["content-type"] == "text/plain; charset=utf-8"
    assert md_resp.text == "# 知研中文标题\n正文内容"

    docx_resp = client.get(f"/api/v1/documents/{docx_id}/content")
    assert docx_resp.status_code == 415
    assert docx_resp.json()["error"]["code"] == "document_content_unsupported"
    assert docx_resp.headers["cache-control"] == "private, no-store"


def test_durable_fence_hides_content_and_chunk_before_physical_removal(preview_host):
    client, backend, path, _, _ = preview_host
    client.cookies.set("zhiyan_session", "owner")
    deleted = client.delete(f"/api/v1/documents/{DOCUMENT}", headers={"X-CSRF-Token": "owner-csrf"})
    assert deleted.status_code == 202
    assert path.is_file()
    assert client.get(PREVIEW).status_code == 404
    assert client.get(CONTENT, headers={"Range": "bytes=0-1"}).status_code == 404
    backend.assert_not_called()


def test_chunk_fetch_discards_result_when_document_is_fenced_mid_read(preview_host):
    client, backend, _, deletion_repository, _ = preview_host
    client.cookies.set("zhiyan_session", "owner")
    result = backend.return_value

    def fetch_and_fence(*_):
        deletion_repository.create_document_deletion("owner", DOCUMENT)
        return result

    backend.side_effect = fetch_and_fence
    assert client.get(PREVIEW).status_code == 404


def test_content_stream_stops_after_fence_commits(preview_host):
    _, _, path, deletion_repository, library = preview_host
    path.write_bytes(b"a" * (1024 * 1024 + 1))
    original = library.get_original_content("owner", DOCUMENT)
    stream = library.read_original_content("owner", original, 0, original.size_bytes)
    assert len(next(stream)) == 1024 * 1024
    deletion_repository.create_document_deletion("owner", DOCUMENT)
    assert list(stream) == []
