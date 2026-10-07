"""
app/routers/series.py — CRUD-like эндпоинты для сериалов
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

from app.db.neo4j import neo4j_driver
from app.schemas import blank_to_none
from app.services.lucene import to_lucene_prefix_query

router = APIRouter(tags=["series"])


class SeriesDetail(BaseModel):
    tmdb_id: int
    name: str
    original_name: str | None = None
    # Cross-ref'ы: "" в Neo4j → null в ответе
    imdb_id: str | None = None
    wikidata_id: str | None = None
    tvdb_id: int | None = None
    start_year: int | None = None
    end_year: int | None = None
    status: str | None = None
    type: str | None = None
    in_production: bool | None = None
    original_language: str | None = None      # ISO 639-1
    season_count: int | None = None
    episode_count: int | None = None
    episode_runtime: float | None = None
    popularity: float | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    us_content_rating: str | None = None
    overview: str | None = None
    tagline: str | None = None
    homepage: str | None = None
    poster_path: str | None = None            # https://image.tmdb.org/t/p/w500{poster_path}
    genres: list[str] = []                    # названия жанров (английские в проверенном деплое)
    keywords: list[str] = []                  # английские теги
    countries: list[str] = []                 # ISO 3166-1 alpha-2
    languages: list[str] = []                 # ISO 639-1, все языки шоу
    networks: list[dict] = []                 # {network_id, name, country}
    cast_count: int | None = None
    creators: list[dict] = []                 # {person_id, name}
    directors: list[dict] = []                # {person_id, name}

    _blank = field_validator(
        "original_name", "imdb_id", "wikidata_id", "tvdb_id", "status", "type",
        "original_language", "us_content_rating", "overview", "tagline",
        "homepage", "poster_path",
        mode="before",
    )(blank_to_none)


# Свойства Series, которые отдаются в карточке как есть.
_SERIES_FIELDS = [
    "original_name", "imdb_id", "wikidata_id", "tvdb_id", "start_year", "end_year",
    "status", "type", "in_production", "original_language", "season_count",
    "episode_count", "episode_runtime", "popularity", "vote_average", "vote_count",
    "us_content_rating", "overview", "tagline", "homepage", "poster_path",
]


@router.get("/series/{tmdb_id}", response_model=SeriesDetail)
async def get_series(tmdb_id: int):
    """Полная карточка сериала."""
    # Pattern comprehension вместо цепочки OPTIONAL MATCH: иначе cast × keywords ×
    # directors × … даёт декартово произведение в сотни тысяч строк на один сериал.
    cypher = """
    MATCH (s:Series {tmdb_id: $tid})
    RETURN s,
           [(s)-[:HAS_GENRE]->(g:Genre)       | g.name]                        AS genres,
           [(s)-[:HAS_KEYWORD]->(k:Keyword)   | k.name]                        AS keywords,
           [(s)-[:PRODUCED_IN]->(c:Country)   | c.code]                        AS countries,
           [(s)-[:HAS_LANGUAGE]->(l:Language) | l.code]                        AS languages,
           [(s)-[:AIRED_ON]->(n:Network)      | n {.network_id, .name, .country}] AS networks,
           COUNT { (s)<-[:ACTED_IN]-(:Person) }                                AS cast_count,
           [(s)<-[:CREATED]-(cr:Person)       | cr {.person_id, .name}]        AS creators,
           [(s)<-[:DIRECTED]-(d:Person)       | d {.person_id, .name}]         AS directors
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, tid=tmdb_id)
        record = await result.single()

    if not record:
        raise HTTPException(status_code=404, detail=f"Series {tmdb_id} not found")

    s = dict(record["s"])
    return SeriesDetail(
        tmdb_id=s["tmdb_id"],
        name=s.get("name", ""),
        **{f: s.get(f) for f in _SERIES_FIELDS},
        genres=[g for g in record["genres"] if g],
        keywords=[k for k in record["keywords"] if k],
        countries=[c for c in record["countries"] if c],
        languages=[lang for lang in record["languages"] if lang],
        networks=[n for n in record["networks"] if n.get("name")],
        cast_count=record["cast_count"],
        creators=[cr for cr in record["creators"] if cr.get("name")],
        directors=[d for d in record["directors"] if d.get("name")],
    )


@router.get("/series")
async def search_series_by_name(
    name: str = Query(..., min_length=2, description="Поиск по названию (полнотекстовый)"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Быстрый поиск по названию через full-text индекс Neo4j.
    Используется для автодополнения в UI.
    """
    lucene_query = to_lucene_prefix_query(name)
    if not lucene_query:
        return {"query": name, "results": []}

    cypher = """
    CALL db.index.fulltext.queryNodes('series_name_idx', $lucene_q)
    YIELD node, score
    // Точное совпадение названия — первым, затем score, затем популярность (prefix-score почти константен).
    WITH node, score,
         CASE WHEN toLower(node.name) = $q_lc
                OR toLower(coalesce(node.original_name, '')) = $q_lc THEN 0 ELSE 1 END AS rank_exact
    ORDER BY rank_exact, score DESC, coalesce(node.popularity, 0) DESC
    LIMIT $limit
    RETURN node.tmdb_id     AS tmdb_id,
           node.name        AS name,
           node.start_year  AS start_year,
           node.end_year    AS end_year,
           node.poster_path AS poster_path,
           score
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, lucene_q=lucene_query, q_lc=name.strip().lower(), limit=limit)
        rows = await result.data()
    return {"query": name, "results": rows}
