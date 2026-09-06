"""Gradio handler authorization tests.

Covers packet 04 acceptance criteria:

- Every state-changing Gradio handler rejects missing, forged, and
  expired tokens before mutation.
- Negative authorization assertions prove no state changed.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import gradio as gr
import pytest

from app.database import initialize_database
from app.session import SessionRegistry
from app.storage import UserStorage, read_json


# ── helpers ──────────────────────────────────────────────────────────────


def _make_isolated_registry(tmp_path, idle_timeout=None):
    """Create an isolated SessionRegistry backed by tmp_path."""
    db_path = tmp_path / "app.db"
    data_root = tmp_path / "data"
    initialize_database(db_path)
    kwargs = dict(db_path=db_path, storage=UserStorage(data_root))
    if idle_timeout is not None:
        kwargs["idle_timeout"] = idle_timeout
    return SessionRegistry(**kwargs)


def _require_raises_gr_error(handler, *args, match=None):
    """Assert that calling *handler* raises ``gr.Error`` with
    the optional *match* pattern."""
    with pytest.raises(gr.Error) as exc_info:
        handler(*args)
    if match:
        assert any(
            word in str(exc_info.value).lower() for word in match
        ), f"Expected match {match!r} in: {exc_info.value}"


# ── state-changing handlers to test ──────────────────────────────────────
#
# Each tuple: (handler_name, args_for_valid_call)
# We provide a valid session_token so we can test expired/forged variants.
# The first positional arg is always the session_token.

STATE_CHANGING_HANDLERS = [
    ("upload_document",  lambda token: (token, None)),
    ("submit_import_batch", lambda token: (token, [])),
    ("retry_import_task", lambda token: (token, "task-id")),
    ("retry_import_batch_failures", lambda token: (token, "batch-id")),
    ("add_note",         lambda token: (token, "test note", "concept")),
    ("clear_all_notes",  lambda token: (token,)),
    ("ask_pdf",          lambda token: (token, "what is this?", None, "auto")),
    ("start_summary_pdf", lambda token: (token, "summary", [])),
    ("cancel_summary_pdf", lambda token: (token, "task-id")),
    ("search_pdf",       lambda token: (token, "keyword", None)),
    ("generate_citations", lambda token: (token, "keyword", None)),
    ("delete_current_pdf", lambda token: (token, None)),
    ("clear_all_pdfs",   lambda token: (token,)),
    ("quarantine_history", lambda token: (token,)),
    ("quarantine_memory",  lambda token: (token,)),
    ("restore_history",  lambda token: (token, "some-backup-id")),
    ("restore_memory",   lambda token: (token, "some-backup-id")),
    ("export_report_docx", lambda token: (token, None)),
    ("export_report_markdown", lambda token: (token,)),
    ("generate_report",  lambda token: (token,)),
    ("recall_memory",    lambda token: (token, "keyword")),
    ("show_stats",       lambda token: (token,)),
    ("create_learning_plan", lambda token: (token, "Doc | doc", "Plan", "2026-09-05", 30)),
    ("set_learning_task_completed", lambda token: (token, "Doc | doc", "plan", "task", True)),
    ("generate_learning_cards", lambda token: (token, "Doc | doc", 3)),
    ("review_learning_card", lambda token: (token, "Doc | doc", "card", "card", "fuzzy")),
    ("generate_learning_exercises", lambda token: (token, "Doc | doc", 3, "mixed")),
    ("submit_learning_multiple_choice", lambda token: (token, "Doc | doc", "exercise", 0)),
    ("review_learning_short_answer", lambda token: (token, "Doc | doc", "exercise", "exercise", "fuzzy", "answer")),
]

LEARNING_MUTATION_HANDLERS = [item for item in STATE_CHANGING_HANDLERS if "learning" in item[0]]


AUTHENTICATED_READ_HANDLERS = [
    ("refresh_import_batches", lambda token: (token,)),
    ("refresh_import_batch", lambda token: (token, "batch-id")),
    ("refresh_import_batch", lambda token: (token, "")),
    (
        "select_import_task",
        lambda token: (token, "batch-id", SimpleNamespace(index=(0, 0))),
    ),
    (
        "select_import_task",
        lambda token: (token, "", SimpleNamespace(index=(0, 0))),
    ),
]

AUTHENTICATED_IMPORT_HANDLERS = [
    ("upload_document", lambda token: (token, None)),
    ("submit_import_batch", lambda token: (token, [])),
    ("refresh_import_batches", lambda token: (token,)),
    ("refresh_import_batch", lambda token: (token, "batch-id")),
    ("refresh_import_batch", lambda token: (token, "")),
    (
        "select_import_task",
        lambda token: (token, "batch-id", SimpleNamespace(index=(0, 0))),
    ),
    (
        "select_import_task",
        lambda token: (token, "", SimpleNamespace(index=(0, 0))),
    ),
    ("retry_import_task", lambda token: (token, "task-id")),
    ("retry_import_batch_failures", lambda token: (token, "batch-id")),
]


def _get_handler(name):
    """Import a handler function from ui.gradio_app."""
    mod = __import__("ui.gradio_app", fromlist=[name])
    return getattr(mod, name)


# ── tests ────────────────────────────────────────────────────────────────


class TestHandlerRejectsMissingToken:
    """Every state-changing handler rejects an empty token (None or '')."""

    @pytest.mark.parametrize("name,args_fn", STATE_CHANGING_HANDLERS)
    def test_handler_rejects_none_token(self, name, args_fn, tmp_path,
                                        monkeypatch):
        """Handler with token=None must raise gr.Error before mutation."""
        isolated = _make_isolated_registry(tmp_path)
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler(name)
        args = args_fn(None)
        _require_raises_gr_error(handler, *args,
                                 match=["log in"])

    @pytest.mark.parametrize("name,args_fn", STATE_CHANGING_HANDLERS)
    def test_handler_rejects_empty_token(self, name, args_fn, tmp_path,
                                         monkeypatch):
        """Handler with token='' must raise gr.Error before mutation."""
        isolated = _make_isolated_registry(tmp_path)
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler(name)
        args = args_fn("")
        _require_raises_gr_error(handler, *args,
                                 match=["log in"])


class TestHandlerRejectsForgedToken:
    """Every state-changing handler rejects a forged/unknown token."""

    @pytest.mark.parametrize("name,args_fn", STATE_CHANGING_HANDLERS)
    def test_handler_rejects_forged_token(self, name, args_fn, tmp_path,
                                          monkeypatch):
        isolated = _make_isolated_registry(tmp_path)
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler(name)
        args = args_fn("forged-token-never-registered")
        _require_raises_gr_error(handler, *args,
                                 match=["log out", "expired", "log in"])

    @pytest.mark.parametrize("name,args_fn", AUTHENTICATED_READ_HANDLERS)
    def test_read_handler_rejects_forged_token(self, name, args_fn, tmp_path,
                                                monkeypatch):
        isolated = _make_isolated_registry(tmp_path)
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)

        _require_raises_gr_error(
            _get_handler(name),
            *args_fn("forged-token-never-registered"),
            match=["log out", "expired", "log in"],
        )


class TestHandlerRejectsExpiredToken:
    """Every state-changing handler rejects an expired token without
    mutating recovery state."""

    @pytest.mark.parametrize("name,args_fn", STATE_CHANGING_HANDLERS)
    def test_handler_rejects_expired_token(self, name, args_fn, tmp_path,
                                           monkeypatch):
        isolated = _make_isolated_registry(
            tmp_path, idle_timeout=timedelta(seconds=-1),
        )
        token = isolated.register("AuthTest", "correct horse battery")
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler(name)
        args = args_fn(token)
        _require_raises_gr_error(handler, *args,
                                 match=["log out", "expired", "log in"])

    @pytest.mark.parametrize("name,args_fn", AUTHENTICATED_READ_HANDLERS)
    def test_read_handler_rejects_expired_token(self, name, args_fn, tmp_path,
                                                 monkeypatch):
        isolated = _make_isolated_registry(
            tmp_path, idle_timeout=timedelta(seconds=-1),
        )
        token = isolated.register("ReadAuthTest", "correct horse battery")
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)

        _require_raises_gr_error(
            _get_handler(name),
            *args_fn(token),
            match=["log out", "expired", "log in"],
        )


class TestRejectedTokenNoStateChange:
    """When a handler rejects a token, NO state is modified."""

    def test_forged_token_does_not_modify_history(self, tmp_path,
                                                   monkeypatch):
        """A forged token calling a mutation handler must leave every
        user's history unchanged."""
        isolated = _make_isolated_registry(tmp_path)
        token = isolated.register("RealUser", "correct horse battery")
        session = isolated.get_session(token)
        assistant = session.assistant

        # Seed state.
        assistant.add_note("original-note", concept="original")
        history_path = assistant.history_repository.path
        original_bytes = history_path.read_bytes()

        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler("add_note")

        with pytest.raises(gr.Error):
            handler("forged-token", "should-not-persist", "hack")

        # History must be identical.
        assert history_path.read_bytes() == original_bytes, (
            "History was modified by forged-token handler call"
        )

    def test_expired_token_does_not_modify_history(self, tmp_path,
                                                    monkeypatch):
        """An expired token calling a mutation handler must not change
        any user's history."""
        isolated = _make_isolated_registry(tmp_path)
        token = isolated.register("RealUser", "correct horse battery")
        session = isolated.get_session(token)
        assistant = session.assistant

        assistant.add_note("pre-existing", concept="safe")
        history_path = assistant.history_repository.path
        original_bytes = history_path.read_bytes()

        # Expire.
        isolated.idle_timeout = timedelta(seconds=-1)
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        handler = _get_handler("add_note")

        with pytest.raises(gr.Error):
            handler(token, "should-not-persist", "hack")

        assert history_path.read_bytes() == original_bytes, (
            "History was modified by expired-token handler call"
        )

    @pytest.mark.parametrize("name,args_fn", AUTHENTICATED_IMPORT_HANDLERS)
    @pytest.mark.parametrize("token_kind", ["missing", "forged", "expired"])
    def test_rejected_import_handler_changes_no_persistent_state(
        self, name, args_fn, token_kind, tmp_path, monkeypatch,
    ):
        isolated = _make_isolated_registry(tmp_path)
        token = isolated.register("ImportAuthTest", "correct horse battery")
        session = isolated.get_session(token)
        paths = isolated.storage.ensure_user_dirs(session.user_id)
        session.assistant.add_note("unchanged", concept="authorization")

        if token_kind == "missing":
            rejected_token = ""
        elif token_kind == "forged":
            rejected_token = "forged-token"
        else:
            isolated.idle_timeout = timedelta(seconds=-1)
            rejected_token = token

        def snapshot():
            return {
                str(path.relative_to(tmp_path)): path.read_bytes()
                for path in tmp_path.rglob("*")
                if path.is_file()
            }

        before = snapshot()
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)

        handler = _get_handler(name)
        args = args_fn(rejected_token)
        if token_kind == "missing" and name in {
            "refresh_import_batches", "refresh_import_batch", "select_import_task",
        }:
            handler(*args)
        else:
            _require_raises_gr_error(
                handler,
                *args,
                match=["log out", "expired", "log in"],
            )

        assert snapshot() == before
        assert not any(paths.imports.iterdir())

    @pytest.mark.parametrize("name,args_fn", LEARNING_MUTATION_HANDLERS)
    @pytest.mark.parametrize("token_kind", ["missing", "forged", "expired"])
    def test_rejected_learning_mutation_preserves_learning_file(
        self, name, args_fn, token_kind, tmp_path, monkeypatch,
    ):
        isolated = _make_isolated_registry(tmp_path)
        token = isolated.register("LearningAuthTest", "correct horse battery")
        assistant = isolated.get_session(token).assistant
        learning_path = assistant.learning_repository.path
        assistant.learning_repository.save(assistant.learning_repository.load())
        original = learning_path.read_bytes() if learning_path.exists() else None
        rejected = "" if token_kind == "missing" else "forged-token"
        if token_kind == "expired":
            isolated.idle_timeout = timedelta(seconds=-1)
            rejected = token
        monkeypatch.setattr("ui.gradio_app.session_registry", isolated)
        _require_raises_gr_error(_get_handler(name), *args_fn(rejected))
        assert (learning_path.read_bytes() if learning_path.exists() else None) == original
