"""
tests/test_tmdb_schema.py — контракт API под схему TMDB (tmdb_id и т.д.).

Neo4j / Qdrant / embed-сервис подменяются фейками; Cypher здесь не исполняется,
проверяется Python-часть: разбор записей, id узлов, отсев сериалов вне графа.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class FakeNeo4j:
    """Подмена neo4j_driver: session() → async CM, run() отдаёт заготовленные ответы по порядку."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def session(self, **_kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def run(self, cypher, **params):
        self.calls.append((cypher, params))
        resp = self._responses.pop(0)
        return SimpleNamespace(
            single=AsyncMock(return_value=resp if not isinstance(resp, list) else None),
            data=AsyncMock(return_value=resp if isinstance(resp, list) else []),
        )


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _patch_neo4j(module: str, fake: FakeNeo4j):
    return patch(f"{module}.neo4j_driver", fake)


# ── /api/series/{tmdb_id} ─────────────────────────────────

def test_series_id_must_be_int(client):
    assert client.get("/api/series/Q123").status_code == 422


def test_series_not_found(client):
    with _patch_neo4j("app.routers.series", FakeNeo4j(None)):
        assert client.get("/api/series/999999999").status_code == 404


def test_series_detail(client):
    record = {
        "s": {
            "tmdb_id": 1399, "name": "Игра престолов", "original_name": "Game of Thrones",
            "imdb_id": "tt0944947", "wikidata_id": "", "tvdb_id": 121361,
            "start_year": 2011, "end_year": 2019, "status": "Ended", "type": "Scripted",
            "in_production": False, "original_language": "en", "season_count": 8,
            "episode_count": 73, "episode_runtime": 60, "popularity": 500.5,
            "vote_average": 8.4, "vote_count": 20000, "us_content_rating": "TV-MA",
            "overview": "…", "tagline": "", "homepage": "", "poster_path": "/p.jpg",
        },
        "genres": ["Драма"], "keywords": ["based on novel", None],
        "countries": ["US"], "languages": ["en", "es"],
        "networks": [{"network_id": 49, "name": "HBO", "country": "US"}],
        "cast_count": 20,
        "creators": [{"person_id": 9813, "name": "David Benioff"}],
        "directors": [{"person_id": None, "name": None}],
    }
    fake = FakeNeo4j(record)
    with _patch_neo4j("app.routers.series", fake):
        resp = client.get("/api/series/1399")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tmdb_id"] == 1399
    assert body["wikidata_id"] is None          # "" → null
    assert body["tagline"] is None and body["homepage"] is None
    assert body["overview"] == "…"
    assert body["languages"] == ["en", "es"]    # все языки шоу, ISO-коды
    assert body["keywords"] == ["based on novel"]
    assert body["networks"] == [{"network_id": 49, "name": "HBO", "country": "US"}]
    assert body["directors"] == []              # пустые записи отфильтрованы
    assert "summary" not in body
    assert fake.calls[0][1] == {"tid": 1399}


# ── графовый ответ ────────────────────────────────────────

def test_series_graph_namespaces_ids_and_skips_unrequested_types(client):
    # У Series 1399 и Person 1399 совпадают числовые ключи — id узлов не должны коллизить.
    record = {
        "s": {"tmdb_id": 1399, "name": "GoT", "start_year": 2011, "poster_path": "/p.jpg",
              "vote_average": 8.4, "popularity": 500.5},
        "ACTED_IN": [{"person_id": 1399, "name": "Actor"}],
        "HAS_KEYWORD": [{"keyword_id": 7, "name": "dragon"}],
        "PRODUCED_IN": [{"code": "US"}],
    }
    fake = FakeNeo4j(record)
    with _patch_neo4j("app.routers.graph", fake):
        resp = client.get(
            "/api/graph/series/1399",
            params=[("include_types", "ACTED_IN,HAS_KEYWORD"), ("include_types", "PRODUCED_IN")],
        )
    assert resp.status_code == 200
    data = resp.json()

    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"series:1399", "person:1399", "keyword:7", "country:US"}
    by_id = {n["id"]: n for n in data["nodes"]}
    assert by_id["country:US"]["name"] == "US" and by_id["country:US"]["key"] == "US"
    assert by_id["series:1399"]["properties"]["poster_path"] == "/p.jpg"

    edges = {(e["source"], e["target"], e["type"]) for e in data["edges"]}
    assert edges == {
        ("person:1399", "series:1399", "ACTED_IN"),        # Person → Series
        ("series:1399", "keyword:7", "HAS_KEYWORD"),       # Series → Keyword
        ("series:1399", "country:US", "PRODUCED_IN"),
    }

    # В запросе только запрошенные типы, пользовательский ввод в Cypher не попал.
    cypher = fake.calls[0][0]
    assert "AS ACTED_IN" in cypher and "AS HAS_KEYWORD" in cypher and "AS PRODUCED_IN" in cypher
    assert "AS HAS_GENRE" not in cypher and "BASED_ON" not in cypher


def test_series_graph_default_types_include_new_relationships(client):
    fake = FakeNeo4j(None)
    with _patch_neo4j("app.routers.graph", fake):
        assert client.get("/api/graph/series/1").status_code == 404
    cypher = fake.calls[0][0]
    for rel in ("HAS_KEYWORD", "AIRED_ON", "HAS_LANGUAGE", "HAS_GENRE", "PRODUCED_IN",
                "ACTED_IN", "CREATED", "DIRECTED"):
        assert f"AS {rel}" in cypher
    assert "ORIGINAL_LANGUAGE" not in cypher and "Work" not in cypher


def test_series_graph_ignores_unknown_include_types(client):
    fake = FakeNeo4j({"s": {"tmdb_id": 5, "name": "X"}})
    with _patch_neo4j("app.routers.graph", fake):
        resp = client.get("/api/graph/series/5", params={"include_types": "BASED_ON,DROP_TABLE"})
    assert resp.status_code == 200
    assert [n["id"] for n in resp.json()["nodes"]] == ["series:5"]
    assert "BASED_ON" not in fake.calls[0][0]


def test_hub_graph_unknown_type_and_bad_key(client):
    assert client.get("/api/graph/node/work/Q1").status_code == 400     # Work удалён
    assert client.get("/api/graph/node/genre/abc").status_code == 422   # genre_id — целое


def test_hub_graph_keyword(client):
    record = {
        "hub": {"keyword_id": 7, "name": "dragon"},
        "total": 120,
        "series_list": [
            {"tmdb_id": 1399, "name": "GoT", "start_year": 2011, "poster_path": "/p.jpg",
             "vote_average": 8.4, "popularity": 500.5},
            None,
        ],
    }
    fake = FakeNeo4j(record)
    with _patch_neo4j("app.routers.graph", fake):
        resp = client.get("/api/graph/node/keyword/7", params={"limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert [n["id"] for n in data["nodes"]] == ["keyword:7", "series:1399"]
    assert data["nodes"][0]["properties"]["total"] == 120
    assert data["edges"] == [{"source": "series:1399", "target": "keyword:7", "type": "HAS_KEYWORD"}]
    cypher, params = fake.calls[0]
    assert "keyword_id: $key" in cypher and "HAS_KEYWORD" in cypher
    assert params == {"key": 7, "limit": 10}


def test_hub_graph_country_key_is_string(client):
    fake = FakeNeo4j({"hub": {"code": "US"}, "total": 0, "series_list": []})
    with _patch_neo4j("app.routers.graph", fake):
        resp = client.get("/api/graph/node/country/US")
    assert resp.status_code == 200
    assert resp.json()["nodes"][0] == {
        "id": "country:US", "key": "US", "label": "Country", "name": "US",
        "properties": {"total": 0},
    }
    assert fake.calls[0][1]["key"] == "US"
    assert "code: $key" in fake.calls[0][0]


def test_person_graph(client):
    record = {
        "p": {"person_id": 1399, "name": "Actor"},
        "works": [{
            "series": {"tmdb_id": 1399, "name": "GoT", "start_year": 2011},
            "rel": "ACTED_IN",
            "costars": [{"person": {"person_id": 2, "name": "Co"}, "rel": "ACTED_IN"}],
        }],
    }
    with _patch_neo4j("app.routers.person", FakeNeo4j(record)):
        resp = client.get("/api/person/1399/graph")
    data = resp.json()
    assert {n["id"] for n in data["nodes"]} == {"person:1399", "series:1399", "person:2"}
    assert {(e["source"], e["target"]) for e in data["edges"]} == {
        ("person:1399", "series:1399"), ("person:2", "series:1399"),
    }


# ── семантический поиск ───────────────────────────────────

def _hit(tmdb_id: int, score: float):
    # point id == tmdb_id; в payload его дублируем, но код должен опираться на id
    return SimpleNamespace(id=tmdb_id, score=score, payload={"tmdb_id": tmdb_id, "name": f"s{tmdb_id}"})


def _row(tmdb_id: int):
    return {"tmdb_id": tmdb_id, "name": f"s{tmdb_id}", "start_year": 2000, "end_year": None,
            "episode_count": 10, "season_count": 1, "overview": "o", "poster_path": "/x.jpg",
            "vote_average": 7.0, "vote_count": 5, "popularity": 1.0}


def test_search_semantic_drops_series_missing_from_graph_and_overfetches(client):
    hits = [_hit(10, 0.9), _hit(11, 0.8), _hit(12, 0.7), _hit(13, 0.6)]
    qdrant = SimpleNamespace(search=AsyncMock(return_value=hits))
    # keep_in_graph: 11 и 13 в графе нет. enrich: остальные.
    neo = FakeNeo4j([{"tid": 10}, {"tid": 12}], [_row(10), _row(12)])

    with patch("app.routers.search.qdrant_client", qdrant), \
         patch("app.routers.search.embed_text", AsyncMock(return_value=[0.0] * 384)), \
         patch("app.services.enricher.neo4j_driver", neo):
        resp = client.get("/api/search", params={"q": "space western", "limit": 2})

    assert resp.status_code == 200
    body = resp.json()
    assert [r["tmdb_id"] for r in body["results"]] == [10, 12]
    assert body["results"][0]["poster_path"] == "/x.jpg"
    assert "summary" not in body["results"][0] and body["results"][0]["overview"] == "o"
    assert qdrant.search.await_args.kwargs["limit"] == 2 * 3        # запас под отсев вне графа


def test_search_hybrid_anchor_is_first_series_in_graph(client):
    hits = [_hit(10, 0.9), _hit(11, 0.8), _hit(12, 0.7)]
    qdrant = SimpleNamespace(search=AsyncMock(return_value=hits))
    # 10 нет в графе → якорь 11, кандидат 12
    neo_enricher = FakeNeo4j(
        [{"tid": 11}, {"tid": 12}],
        [_row(11), _row(12)],
    )
    neo_search = FakeNeo4j(
        [{"cid": 12, "shared_actors": 2, "shared_countries": 1, "shared_creators": 0}],
    )

    with patch("app.routers.search.qdrant_client", qdrant), \
         patch("app.routers.search.embed_text", AsyncMock(return_value=[0.0] * 384)), \
         patch("app.services.enricher.neo4j_driver", neo_enricher), \
         patch("app.routers.search.neo4j_driver", neo_search):
        resp = client.get("/api/search", params={"q": "space western", "mode": "hybrid"})

    assert resp.status_code == 200
    # 10 вне графа; 12 получил граф-буст и обогнал якорь 11
    assert [r["tmdb_id"] for r in resp.json()["results"]] == [12, 11]
    cypher, params = neo_search.calls[0]
    assert params["source_id"] == 11 and params["candidate_ids"] == [12]
    assert "tmdb_id: $source_id" in cypher


def test_similar_uses_retrieve_by_tmdb_id_and_ints(client):
    point = SimpleNamespace(id=1399, vector=[0.1] * 384, payload={})
    qdrant = SimpleNamespace(
        get_by_id=AsyncMock(return_value=point),
        search=AsyncMock(return_value=[_hit(1399, 1.0), _hit(20, 0.9)]),
    )
    # semantic: keep_in_graph → [20]; граф-добор (кандидатов < порога) → пусто; enrich → 20
    neo_enricher = FakeNeo4j([{"tid": 20}], [_row(20)])
    neo_search = FakeNeo4j([])

    with patch("app.routers.search.qdrant_client", qdrant), \
         patch("app.services.enricher.neo4j_driver", neo_enricher), \
         patch("app.routers.search.neo4j_driver", neo_search):
        resp = client.get("/api/series/1399/similar", params={"mode": "semantic"})

    assert resp.status_code == 200
    body = resp.json()
    assert qdrant.get_by_id.await_args.args == (1399,)
    assert [r["tmdb_id"] for r in body["results"]] == [20]      # сам сериал исключён
    assert body["results"][0]["source"] == "vector"
    fallback_cypher, fallback_params = neo_search.calls[0]
    assert "tmdb_id: $source_id" in fallback_cypher
    assert set(fallback_params["exclude_ids"]) == {20, 1399}


def test_similar_id_must_be_int(client):
    assert client.get("/api/series/Q1/similar").status_code == 422


def test_search_persons_returns_person_id(client):
    fake = FakeNeo4j([{"person_id": 9813, "name": "David Benioff", "score": 3.0, "series_count": 4}])
    with patch("app.routers.search.neo4j_driver", fake):
        resp = client.get("/api/search/persons", params={"q": "benioff"})
    assert resp.json()["results"] == [
        {"person_id": 9813, "name": "David Benioff", "score": 3.0, "series_count": 4},
    ]
