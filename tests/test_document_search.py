import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.document_search import (
    DocumentSearchService, DocumentSearchRequest, SearchChunkLocator,
    DocumentSearchValidationError, DocumentSearchScopeError,
    DocumentSearchScopeChangedError, DocumentSearchUnavailableError,
    DocumentSearchBusyError, DocumentSearchSourceStaleError,
)


DOC = str(uuid4())
HASH = hashlib.sha256(b"source").hexdigest()


def hit(**changes):
    return dict(document_id=DOC, chunk_id=f"{DOC}_0", chunk_index=0,
                content="source", content_sha256=HASH, score=0.9,
                page_number=2, section="Introduction") | changes


def envelope(**data):
    return SimpleNamespace(success=True, data=data, error_code="")


def setup_service(execute=None, capacity=4):
    library = {"alice": [SimpleNamespace(document_id=DOC, name="论文.pdf")], "bob": []}
    tool = SimpleNamespace(execute_result=execute or (lambda *a, **kw: envelope(results=[hit()])))
    sessions = SimpleNamespace(get_session=lambda token: SimpleNamespace(user_id=token, runtime=SimpleNamespace(rag_tool=tool)))
    documents = SimpleNamespace(list_documents=lambda token: library[token])
    return DocumentSearchService(sessions, documents, max_concurrent=capacity), library, tool


@pytest.mark.parametrize("changes", [
    {"query": " "}, {"query": "x" * 1001}, {"document_ids": ()},
    {"document_ids": (DOC, DOC)}, {"document_ids": ("not-a-uuid",)},
    {"document_ids": tuple(str(uuid4()) for _ in range(11))}, {"limit": 1},
])
def test_invalid_requests(changes):
    with pytest.raises(DocumentSearchValidationError):
        DocumentSearchRequest(**(dict(query="source", document_ids=(DOC,)) | changes))


def test_result_is_structured_and_uses_authoritative_name():
    service, _, _ = setup_service()
    result = service.search("alice", DocumentSearchRequest(" source ", (DOC,)))
    assert result.result_count == 1
    assert result.results[0].document_name == "论文.pdf"
    assert result.results[0].locator.content_sha256 == HASH
    assert result.results[0].locator.page_number == 2
    assert result.results[0].rank == 1


def test_foreign_or_missing_scope_is_rejected_before_backend():
    service, _, tool = setup_service()
    tool.execute_result = lambda *a, **kw: pytest.fail("must not call backend")
    with pytest.raises(DocumentSearchScopeError):
        service.search("bob", DocumentSearchRequest("source", (DOC,)))


def test_deletion_during_search_discards_results():
    service, library, tool = setup_service()
    def execute(*a, **kw):
        library["alice"] = []
        return envelope(results=[hit()])
    tool.execute_result = execute
    with pytest.raises(DocumentSearchScopeChangedError):
        service.search("alice", DocumentSearchRequest("source", (DOC,)))
    assert service._active_users == set()


@pytest.mark.parametrize("bad", [
    hit(document_id=str(uuid4())), hit(chunk_index=-1), hit(content_sha256="bad"),
    hit(score=float("nan")), hit(score="bad"), hit(page_number=0), None,
])
def test_malformed_or_out_of_scope_backend_result_fails_closed(bad):
    service, _, _ = setup_service(lambda *a, **kw: envelope(results=[bad]))
    with pytest.raises(DocumentSearchUnavailableError):
        service.search("alice", DocumentSearchRequest("source", (DOC,)))


def test_timeout_releases_admission_and_can_retry():
    def fail(*a, **kw):
        raise TimeoutError("secret endpoint")
    service, _, tool = setup_service(fail)
    with pytest.raises(DocumentSearchUnavailableError):
        service.search("alice", DocumentSearchRequest("source", (DOC,)))
    tool.execute_result = lambda *a, **kw: envelope(results=[])
    assert service.search("alice", DocumentSearchRequest("source", (DOC,))).result_count == 0


def test_user_and_global_admission_and_release():
    entered, release = Event(), Event()
    def wait(*a, **kw):
        entered.set()
        assert release.wait(5)
        return envelope(results=[])
    service, library, tool = setup_service(wait, capacity=1)
    library["bob"] = library["alice"]
    request = DocumentSearchRequest("source", (DOC,))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(service.search, "alice", request)
        try:
            assert entered.wait(5)
            for user in ("alice", "bob"):
                with pytest.raises(DocumentSearchBusyError):
                    service.search(user, request)
        finally:
            release.set()
        assert future.result().result_count == 0
    tool.execute_result = lambda *a, **kw: envelope(results=[])
    assert service.search("bob", request).result_count == 0


def test_source_resolver_binds_all_identity_fields_and_hash():
    service, _, tool = setup_service(lambda *a, **kw: envelope(chunk=hit()))
    locator = SearchChunkLocator(DOC, f"{DOC}_0", 0, HASH)
    assert service.resolve_chunk("alice", locator).content == "source"
    for changes in ({"content_sha256": "0" * 64}, {"chunk_id": "wrong"}, {"chunk_index": 1}, {"document_id": str(uuid4())}):
        tool.execute_result = lambda *a, changes=changes, **kw: envelope(chunk=hit(**changes))
        with pytest.raises(DocumentSearchSourceStaleError):
            service.resolve_chunk("alice", locator)


def test_source_backend_failure_is_not_reported_as_deleted_source():
    service, _, _ = setup_service(lambda *a, **kw: SimpleNamespace(success=False, data={}))
    with pytest.raises(DocumentSearchUnavailableError):
        service.resolve_chunk("alice", SearchChunkLocator(DOC, f"{DOC}_0", 0, HASH))
