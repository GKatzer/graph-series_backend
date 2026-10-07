"""
tests/test_behaviour.py — поведение, добавленное после разбора API: числа общих актёров/стран в ответе,
порядок structural-выдачи, source, ошибки на английском, 503 при недоступном embedding-сервисе,
проверка full-text индексов в /health/full. Базы подменены, как в остальных тестах.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.services.embedder import EmbeddingUnavailable
from tests.test_tmdb_schema import FakeNeo4j, _hit, _row


@pytest.fixture(autouse=True)
def mock_dbs(monkeypatch):
    """Подмена драйверов ДО первого импорта app.main (lifespan проверяет связь с БД), как в test_health.py."""
    from unittest.mock import MagicMock

    neo = MagicMock(verify_connectivity=AsyncMock(), close=AsyncMock(), session=AsyncMock())
    qdr = MagicMock(verify_connectivity=AsyncMock())
    monkeypatch.setattr("app.db.neo4j.neo4j_driver", neo)
    monkeypatch.setattr("app.db.qdrant.qdrant_client", qdr)


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


# ── shared_actors / shared_countries ──────────────────────

def _hybrid_search(client):
    hits = [_hit(11, 0.8), _hit(12, 0.7)]
    qdrant = SimpleNamespace(search=AsyncMock(return_value=hits))
    neo_enricher = FakeNeo4j([{"tid": 11}, {"tid": 12}], [_row(11), _row(12)])
    neo_search = FakeNeo4j([{"cid": 12, "shared_actors": 2, "shared_countries": 1, "shared_creators": 0}])
    with patch("app.routers.search.qdrant_client", qdrant), \
         patch("app.routers.search.embed_text", AsyncMock(return_value=[0.0] * 384)), \
         patch("app.services.enricher.neo4j_driver", neo_enricher), \
         patch("app.routers.search.neo4j_driver", neo_search):
        return client.get("/api/search", params={"q": "space western", "mode": "hybrid"})


def test_hybrid_search_returns_shared_counts_and_null_for_the_anchor(client):
    by_id = {r["tmdb_id"]: r for r in _hybrid_search(client).json()["results"]}
    assert (by_id[12]["shared_actors"], by_id[12]["shared_countries"]) == (2, 1)
    assert by_id[11]["shared_actors"] is None and by_id[11]["shared_countries"] is None   # anchor: nothing to compare


def test_hybrid_score_includes_the_bonus(client):
    by_id = {r["tmdb_id"]: r for r in _hybrid_search(client).json()["results"]}
    assert by_id[12]["score"] == pytest.approx(0.7 + 2 * 0.05 + 1 * 0.1)


def test_similar_hybrid_returns_shared_counts(client):
    point = SimpleNamespace(id=1399, vector=[0.1] * 384, payload={})
    qdrant = SimpleNamespace(get_by_id=AsyncMock(return_value=point),
                             search=AsyncMock(return_value=[_hit(1399, 1.0)] + [_hit(i, 0.9) for i in range(20, 31)]))
    rerank_rows = [{"cid": i, "shared_actors": i - 20, "shared_countries": 0, "shared_creators": 0} for i in range(20, 31)]
    neo_search = FakeNeo4j(rerank_rows)
    neo_enricher = FakeNeo4j([_row(i) for i in range(20, 31)])
    with patch("app.routers.search.qdrant_client", qdrant), \
         patch("app.services.enricher.neo4j_driver", neo_enricher), \
         patch("app.routers.search.neo4j_driver", neo_search):
        resp = client.get("/api/series/1399/similar", params={"mode": "hybrid"})
    assert resp.status_code == 200
    by_id = {r["tmdb_id"]: r for r in resp.json()["results"]}
    assert by_id[23]["shared_actors"] == 3 and by_id[23]["shared_countries"] == 0


# ── structural search ─────────────────────────────────────

def test_structural_results_are_marked_as_graph_and_ordered_by_exact_match_first(client):
    fake = FakeNeo4j([{**_row(1396), "score": 2.0}])
    with patch("app.routers.search.neo4j_driver", fake):
        resp = client.get("/api/search", params={"q": "  Breaking Bad ", "mode": "structural"})
    assert resp.json()["results"][0]["source"] == "graph"
    cypher, params = fake.calls[0]
    assert params["q_lc"] == "breaking bad"
    assert "rank_exact" in cypher and cypher.index("ORDER BY rank_exact") < cypher.index("LIMIT $limit")


def test_autocomplete_and_person_search_also_prefer_exact_matches(client):
    fake = FakeNeo4j([])
    with patch("app.routers.series.neo4j_driver", fake):
        client.get("/api/series", params={"name": "Vinyl"})
    assert fake.calls[0][1]["q_lc"] == "vinyl" and "ORDER BY rank_exact" in fake.calls[0][0]

    fake = FakeNeo4j([])
    with patch("app.routers.search.neo4j_driver", fake):
        client.get("/api/search/persons", params={"q": "Bryan Cranston"})
    assert fake.calls[0][1]["q_lc"] == "bryan cranston" and "ORDER BY rank_exact" in fake.calls[0][0]


# ── error messages ────────────────────────────────────────

def test_hub_errors_are_in_english(client):
    unknown = client.get("/api/graph/node/work/1").json()["detail"]
    bad_key = client.get("/api/graph/node/genre/abc").json()["detail"]
    assert unknown.startswith("Unknown node type: 'work'") and "genre" in unknown
    assert bad_key == "Genre key must be an integer, got 'abc'"


# ── embedding service outage ──────────────────────────────

def test_search_returns_503_when_the_embedding_service_is_down(client):
    with patch("app.routers.search.embed_text", AsyncMock(side_effect=EmbeddingUnavailable("down"))):
        resp = client.get("/api/search", params={"q": "space western"})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Embedding service unavailable"}


def test_embedder_wraps_any_failure_in_embedding_unavailable():
    import asyncio

    from app.services import embedder

    with patch.object(embedder._client, "post", AsyncMock(side_effect=RuntimeError("boom"))):
        with pytest.raises(EmbeddingUnavailable):
            asyncio.run(embedder.embed_text("x"))


def test_stream_reports_an_embedding_failure_as_an_error_event(client):
    with patch("app.routers.search.embed_text", AsyncMock(side_effect=EmbeddingUnavailable("down"))):
        resp = client.get("/api/search/stream", params={"q": "space western"})
    assert resp.status_code == 200
    assert '"type": "error"' in resp.text


# ── /health/full ──────────────────────────────────────────

def _health(client, neo: FakeNeo4j, qdrant_ok: bool = True):
    qdrant = SimpleNamespace(verify_connectivity=AsyncMock(side_effect=None if qdrant_ok else RuntimeError("no qdrant")))
    with patch("app.routers.health.neo4j_driver", neo), patch("app.routers.health.qdrant_client", qdrant):
        return client.get("/health/full")


_ONLINE = [{"name": "series_name_idx", "state": "ONLINE"}, {"name": "person_name_idx", "state": "ONLINE"}]


def test_health_full_ok_when_both_fulltext_indexes_are_online(client):
    resp = _health(client, FakeNeo4j([], _ONLINE))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "neo4j": "up", "qdrant": "up", "fulltext_indexes": "online"}


def test_health_full_fails_when_a_fulltext_index_is_missing(client):
    resp = _health(client, FakeNeo4j([], [{"name": "series_name_idx", "state": "ONLINE"}]))
    assert resp.status_code == 503
    msg = resp.json()["detail"]["errors"]["fulltext_indexes"]
    assert "person_name_idx" in msg and "MISSING" in msg and "schema_init.cypher" in msg


def test_health_full_fails_while_an_index_is_populating(client):
    populating = [{"name": "series_name_idx", "state": "POPULATING"}, {"name": "person_name_idx", "state": "ONLINE"}]
    resp = _health(client, FakeNeo4j([], populating))
    assert resp.status_code == 503
    assert "POPULATING" in resp.json()["detail"]["errors"]["fulltext_indexes"]


def test_health_full_reports_qdrant_failure(client):
    resp = _health(client, FakeNeo4j([], _ONLINE), qdrant_ok=False)
    assert resp.status_code == 503
    assert "qdrant" in resp.json()["detail"]["errors"]
