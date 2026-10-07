"""
tests/test_lucene.py — экранирование пользовательского ввода в Lucene-запросах
(full-text поиск через Neo4j: /api/search, /api/search/persons, /api/series).

Без экранирования спецсимволы (например, непарная скобка) ломают разбор
запроса на стороне Neo4j: было 500 вместо результата поиска.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.services.lucene import to_lucene_prefix_query
from tests.test_tmdb_schema import FakeNeo4j


@pytest.mark.parametrize("raw, expected", [
    ("breaking (bad", 'breaking* AND \\(bad*'),
    ('say "hi"', 'say* AND \\"hi\\"*'),
    ("time: now", 'time\\:* AND now*'),
    ("a+b -c", 'a\\+b* AND \\-c*'),
    ("wild*card?", 'wild\\*card\\?*'),
    ("", ""),
    ("   ", ""),
])
def test_to_lucene_prefix_query_escapes_special_chars(raw, expected):
    assert to_lucene_prefix_query(raw) == expected


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def test_structural_search_escapes_query(client):
    fake = FakeNeo4j([])
    with patch("app.routers.search.neo4j_driver", fake):
        resp = client.get("/api/search", params={"q": "breaking (bad", "mode": "structural"})
    assert resp.status_code == 200
    assert fake.calls[0][1]["lucene_q"] == 'breaking* AND \\(bad*'


def test_search_persons_escapes_query(client):
    fake = FakeNeo4j([])
    with patch("app.routers.search.neo4j_driver", fake):
        resp = client.get("/api/search/persons", params={"q": 'say "hi"'})
    assert resp.status_code == 200
    assert fake.calls[0][1]["lucene_q"] == 'say* AND \\"hi\\"*'


def test_search_series_by_name_escapes_query(client):
    fake = FakeNeo4j([])
    with patch("app.routers.series.neo4j_driver", fake):
        resp = client.get("/api/series", params={"name": "time: now"})
    assert resp.status_code == 200
    assert fake.calls[0][1]["lucene_q"] == 'time\\:* AND now*'


def test_structural_search_whitespace_only_query_skips_neo4j(client):
    fake = FakeNeo4j()
    with patch("app.routers.search.neo4j_driver", fake):
        resp = client.get("/api/search", params={"q": "  ", "mode": "structural"})
    assert resp.status_code == 200
    assert resp.json()["results"] == []
    assert fake.calls == []
