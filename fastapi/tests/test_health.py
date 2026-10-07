"""
tests/test_health.py — smoke-тесты без реальных БД (mock).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# Патчим подключения к БД ДО импорта приложения
@pytest.fixture(autouse=True)
def mock_dbs(monkeypatch):
    # Neo4j
    mock_driver = MagicMock()
    mock_driver.verify_connectivity = AsyncMock()
    mock_driver.close = AsyncMock()
    mock_driver.session = AsyncMock()

    # Qdrant
    mock_qdrant = MagicMock()
    mock_qdrant.verify_connectivity = AsyncMock()

    monkeypatch.setattr("app.db.neo4j.neo4j_driver", mock_driver)
    monkeypatch.setattr("app.db.qdrant.qdrant_client", mock_qdrant)
    yield


@pytest.fixture
def client():
    # Импортируем app ПОСЛЕ патча
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "uptime_s" in data


def test_series_not_found(client):
    with patch("app.routers.series.neo4j_driver") as mock:
        session_mock = AsyncMock()
        result_mock = AsyncMock()
        result_mock.single = AsyncMock(return_value=None)
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock.run = AsyncMock(return_value=result_mock)
        mock.session = AsyncMock(return_value=session_mock)

        resp = client.get("/api/series/9999999999")
        assert resp.status_code == 404


def test_search_requires_query(client):
    resp = client.get("/api/search")
    assert resp.status_code == 422   # validation error — q обязателен


def test_search_min_length(client):
    resp = client.get("/api/search?q=x")
    assert resp.status_code == 422   # min_length=2
