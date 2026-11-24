from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest
from fastapi.testclient import TestClient

from api.app import create_api_app
from api.config import ApiConfig
from app.bootstrap import ApplicationServices

from tests.api.test_auth_routes import FakeServices, FakeSessionRegistry


def test_health_and_lifespan_start_and_stop_shared_services_once():
    services = FakeServices(session_registry=FakeSessionRegistry())
    api_app = create_api_app(services)

    with TestClient(api_app) as client:
        assert services.start_calls == 1
        assert services.stop_calls == 0
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/healthz").json() == {"status": "ok"}
        assert services.start_calls == 1
        assert api_app.state.services is services

    assert services.stop_calls == 1


def test_service_startup_can_call_synchronous_embedding(monkeypatch):
    services = FakeServices(session_registry=FakeSessionRegistry())
    start = services.start

    def start_outside_event_loop():
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        start()

    monkeypatch.setattr(services, "start", start_outside_event_loop)
    with TestClient(create_api_app(services)) as client:
        assert client.get("/healthz").status_code == 200
        assert services.start_calls == 1


def test_api_config_defaults_and_true_only_environment_parsing(monkeypatch):
    monkeypatch.delenv("APP_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("QA_ROUTE_ENABLED", raising=False)
    assert ApiConfig.from_environment() == ApiConfig(
        cookie_name="zhiyan_session",
        cookie_secure=False,
        cookie_samesite="lax",
        qa_route_enabled=True,
    )

    monkeypatch.setenv("APP_COOKIE_SECURE", "TRUE")
    assert ApiConfig.from_environment().cookie_secure is True

    monkeypatch.setenv("APP_COOKIE_SECURE", "1")
    assert ApiConfig.from_environment().cookie_secure is False

    monkeypatch.setenv("QA_ROUTE_ENABLED", "FALSE")
    assert ApiConfig.from_environment().qa_route_enabled is False
    monkeypatch.setenv("QA_ROUTE_ENABLED", "0")
    assert ApiConfig.from_environment().qa_route_enabled is True


def test_api_config_is_frozen():
    config = ApiConfig()

    with pytest.raises(FrozenInstanceError):
        config.cookie_secure = True


def test_route_off_does_not_skip_note_migration_or_projection_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_ROUTE_ENABLED", "false")
    services = ApplicationServices.create(tmp_path / "data")
    calls = []
    services.note_migration.migrate_known_users = lambda: calls.append("migrate")
    services.note_projection_repository.recover_expired = lambda: calls.append("recover")
    for worker in (
        services.import_worker_pool,
        services.qa_worker_pool,
        services.qa_deletion_worker,
        services.note_projection_worker,
    ):
        worker.start = lambda: calls.append("start")
        worker.stop = lambda: calls.append("stop")
    with TestClient(create_api_app(services)) as client:
        assert client.app.state.api_config.notes_route_enabled is False
    assert calls[:2] == ["migrate", "recover"]
