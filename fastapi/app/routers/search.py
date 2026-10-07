"""
app/routers/search.py — семантический и гибридный поиск
"""
from __future__ import annotations

import asyncio
import json
import logging
from enum import Enum
from typing import AsyncIterator

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db.neo4j import neo4j_driver
from app.db.qdrant import qdrant_client
from app.schemas import blank_to_none
from app.services.embedder import embed_text
from app.services.enricher import enrich_series_list, keep_in_graph
from app.services.lucene import to_lucene_prefix_query

log = logging.getLogger("search")
router = APIRouter(tags=["search"])


class SearchMode(str, Enum):
    semantic   = "semantic"    # только векторный поиск
    structural = "structural"  # только граф (по имени → похожие)
    hybrid     = "hybrid"      # semantic + re-rank по графу


class SeriesResult(BaseModel):
    tmdb_id: int
    name: str
    start_year: int | None
    end_year: int | None
    episode_count: int | None
    season_count: int | None
    overview: str | None
    poster_path: str | None = None     # https://image.tmdb.org/t/p/w500{poster_path}
    vote_average: float | None = None
    vote_count: int | None = None
    popularity: float | None = None
    score: float
    shared_actors: int | None = None   # для hybrid
    shared_countries: int | None = None
    source: str = "vector"             # "vector" | "graph" — откуда пришёл кандидат


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    results: list[SeriesResult]
    total: int


class PersonResult(BaseModel):
    person_id: int
    name: str
    score: float
    series_count: int


class PersonSearchResponse(BaseModel):
    query: str
    results: list[PersonResult]
    total: int


# В Qdrant лежат все сериалы с текстом (~211k), а в Neo4j по умолчанию только
# vote_count >= 2 (~56k). Кандидатов из Qdrant берём с запасом, чтобы после
# отсева сериалов, которых нет в графе, осталось достаточно.
GRAPH_COVERAGE_OVERFETCH = 3


def _to_result(row: dict, score: float, source: str = "vector") -> SeriesResult:
    return SeriesResult(
        tmdb_id=row["tmdb_id"],
        name=row.get("name") or "",
        start_year=row.get("start_year"),
        end_year=row.get("end_year"),
        episode_count=row.get("episode_count"),
        season_count=row.get("season_count"),
        overview=blank_to_none(row.get("overview")),
        poster_path=blank_to_none(row.get("poster_path")),
        vote_average=row.get("vote_average"),
        vote_count=row.get("vote_count"),
        popularity=row.get("popularity"),
        score=score,
        shared_actors=row.get("shared_actors"),
        shared_countries=row.get("shared_countries"),
        source=source,
    )


def _attach_shared(rows: list[dict], shared: dict[int, tuple[int, int]]) -> None:
    """Кладёт числа общих актёров/стран (из hybrid re-rank) в карточки; у якоря их нет — остаётся null."""
    for row in rows:
        counts = shared.get(row["tmdb_id"])
        if counts is not None:
            row["shared_actors"], row["shared_countries"] = counts


# ── Семантический поиск ───────────────────────────────────

@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=2, max_length=500, description="Поисковый запрос"),
    limit: int = Query(10, ge=1, le=50),
    mode: SearchMode = SearchMode.semantic,
    score_threshold: float = Query(0.0, ge=0.0, le=1.0),
    actor_boost: float = Query(0.05, ge=0.0, le=1.0, description="Прирост скора за каждого общего актёра (hybrid)"),
    country_boost: float = Query(0.1, ge=0.0, le=1.0, description="Прирост скора за общую страну (hybrid)"),
):
    """
    Поиск сериалов.

    - **semantic** — векторный поиск по описаниям
    - **hybrid** — semantic + boost за общих актёров/страны с топ-результатом
    """
    if mode == SearchMode.structural:
        return await _structural_search(q=q, limit=limit)

    vector = await embed_text(q)
    hits = await qdrant_client.search(
        vector, limit=limit * GRAPH_COVERAGE_OVERFETCH, score_threshold=score_threshold,
    )

    if not hits:
        return SearchResponse(query=q, mode=mode, results=[], total=0)

    # point id == tmdb_id. Оставляем только сериалы, которые есть в графе:
    # якорь hybrid и все карточки должны быть доступны для навигации.
    tmdb_ids = await keep_in_graph([int(h.id) for h in hits])
    score_map = {int(h.id): h.score for h in hits}

    if not tmdb_ids:
        return SearchResponse(query=q, mode=mode, results=[], total=0)

    shared_map: dict[int, tuple[int, int]] = {}

    # Hybrid: якорь — топ-1 из Qdrant, по нему ре-ранкируем остальных графом
    if mode == SearchMode.hybrid and len(tmdb_ids) > 1:
        anchor_id    = tmdb_ids[0]
        candidate_ids = tmdb_ids[1:]
        candidate_ids, score_map = await _hybrid_rerank(
            source_id=anchor_id,
            candidate_ids=candidate_ids,
            score_map=score_map,
            actor_boost=actor_boost,
            country_boost=country_boost,
            shared=shared_map,
        )
        tmdb_ids = [anchor_id] + candidate_ids

    enriched = await enrich_series_list(tmdb_ids[:limit])
    _attach_shared(enriched, shared_map)

    results = [_to_result(row, score_map.get(row["tmdb_id"], 0.0)) for row in enriched]

    results.sort(key=lambda x: x.score, reverse=True)
    return SearchResponse(query=q, mode=mode, results=results[:limit], total=len(results))


# ── SSE: прогрессивная выдача ─────────────────────────────

@router.get("/search/stream")
async def search_stream(
    q: str = Query(..., min_length=2, max_length=500),
    limit: int = Query(10, ge=1, le=50),
    score_threshold: float = Query(0.0, ge=0.0, le=1.0),
):
    """
    Server-Sent Events: результаты доставляются по мере готовности.
    Клиент получает каждый сериал сразу после обогащения из Neo4j.
    """
    async def event_stream() -> AsyncIterator[str]:
        try:
            vector = await embed_text(q)
            hits = await qdrant_client.search(
                vector, limit=limit * GRAPH_COVERAGE_OVERFETCH, score_threshold=score_threshold,
            )

            if not hits:
                yield _sse({"type": "done", "total": 0})
                return

            # meta.total — верхняя оценка: часть кандидатов может отсеяться (нет в графе)
            yield _sse({"type": "meta", "total": min(len(hits), limit)})

            # Обогащаем по одному и стримим; сериалы вне графа пропускаем
            sent = 0
            for hit in hits:
                rows = await enrich_series_list([int(hit.id)])
                if rows:
                    row = rows[0]
                    row["score"] = hit.score
                    yield _sse({"type": "result", "data": row})
                    sent += 1
                    if sent >= limit:
                        break
                await asyncio.sleep(0)   # отдаём управление event loop

            yield _sse({"type": "done", "total": sent})

        except Exception as exc:
            log.exception("SSE search error: %s", exc)
            yield _sse({"type": "error", "message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── Похожие на конкретный сериал ──────────────────────────

# Цель и порог добора графом для /similar.
# Если векторных кандидатов < SIMILAR_FILL_THRESHOLD — добираем графом до SIMILAR_MAX.
# Иначе отдаём только векторные результаты (но не больше SIMILAR_MAX).
SIMILAR_FILL_THRESHOLD = 10
SIMILAR_MAX = 20

# Веса структурных сигналов при графовом доборе (актёры доминируют).
GRAPH_W_ACTOR   = 1.0
GRAPH_W_CREATOR = 3.0
GRAPH_W_GENRE   = 0.1
GRAPH_W_COUNTRY = 0.05


@router.get("/series/{tmdb_id}/similar", response_model=SearchResponse)
async def similar_series(
    tmdb_id: int,
    mode: SearchMode = SearchMode.hybrid,
    exclude_same_creator: bool = False,
    same_country_only: bool = False,
    actor_boost: float = Query(0.05, ge=0.0, le=1.0, description="Прирост скора за каждого общего актёра (hybrid)"),
    country_boost: float = Query(0.1, ge=0.0, le=1.0, description="Прирост скора за общую страну (hybrid)"),
):
    """
    Найти сериалы, похожие на указанный.

    Стратегии:
    - **semantic** — по близости векторов описаний
    - **hybrid** — semantic + учёт структурных связей (каст, страна)

    Гарантия непустого ответа: если векторных результатов меньше
    `SIMILAR_FILL_THRESHOLD` (10) — добираем структурными кандидатами из графа
    (общие актёры/создатели/жанры/страна) до `SIMILAR_MAX` (20). Работает и
    когда сериала вообще нет в векторной БД — лишь бы он был в Neo4j.
    """
    target = SIMILAR_MAX

    # ── 1. Векторная фаза (может быть пустой, если точки нет в Qdrant) ──
    vector_ids: list[int] = []
    score_map: dict[int, float] = {}
    shared_map: dict[int, tuple[int, int]] = {}

    point = await qdrant_client.get_by_id(tmdb_id)
    if point and point.vector:
        # С запасом: часть кандидатов из Qdrant может отсеяться (нет в графе).
        hits = await qdrant_client.search(point.vector, limit=target * 4)
        hits = [h for h in hits if int(h.id) != tmdb_id]

        if hits:
            vector_ids = [int(h.id) for h in hits]
            score_map  = {int(h.id): h.score for h in hits}

            if mode == SearchMode.hybrid:
                # _hybrid_rerank сам отбрасывает кандидатов, которых нет в графе
                vector_ids, score_map = await _hybrid_rerank(
                    source_id=tmdb_id,
                    candidate_ids=vector_ids,
                    score_map=score_map,
                    exclude_same_creator=exclude_same_creator,
                    same_country_only=same_country_only,
                    actor_boost=actor_boost,
                    country_boost=country_boost,
                    shared=shared_map,
                )
            else:
                vector_ids = await keep_in_graph(vector_ids)

    vector_ids = vector_ids[:target]

    # ── 2. Графовый добор, если векторных кандидатов мало ──
    graph_ids: list[int] = []
    if len(vector_ids) < SIMILAR_FILL_THRESHOLD:
        need = target - len(vector_ids)
        exclude = set(vector_ids) | {tmdb_id}
        graph_pairs = await _graph_fallback(tmdb_id, exclude, need)
        for cid, overlap in graph_pairs:
            graph_ids.append(cid)
            score_map[cid] = overlap

    # ── 3. Обогащение и сборка (порядок: вектор → граф) ──
    ordered_ids = vector_ids + graph_ids
    if not ordered_ids:
        return SearchResponse(query=str(tmdb_id), mode=mode, results=[], total=0)

    graph_set = set(graph_ids)
    enriched = await enrich_series_list(ordered_ids)
    _attach_shared(enriched, shared_map)
    results = [
        _to_result(
            r,
            score_map.get(r["tmdb_id"], 0.0),
            source="graph" if r["tmdb_id"] in graph_set else "vector",
        )
        for r in enriched
    ]
    # enrich_series_list сохраняет порядок ordered_ids, поэтому векторные идут
    # первыми (по своему скору), графовые — следом (по структурному overlap).
    return SearchResponse(query=str(tmdb_id), mode=mode, results=results, total=len(results))


async def _graph_fallback(
    source_id: int,
    exclude_ids: set[int],
    need: int,
) -> list[tuple[int, float]]:
    """
    Структурный добор кандидатов из графа, когда векторов не хватает.

    Ранжирует по взвешенной сумме общих актёров / создателей / жанров / страны.
    Агрегация выполняется на стороне Neo4j, кандидаты ограничены LIMIT —
    чтобы не разворачивать миллионный каст в память приложения.
    """
    if need <= 0:
        return []

    cypher = """
    MATCH (src:Series {tmdb_id: $source_id})
    CALL {
        WITH src
        MATCH (src)<-[:ACTED_IN]-(p:Person)-[:ACTED_IN]->(cand:Series)
        WHERE NOT cand.tmdb_id IN $exclude_ids
        RETURN cand.tmdb_id AS cid, count(DISTINCT p) * $w_actor AS s
        UNION ALL
        WITH src
        MATCH (src)<-[:CREATED]-(p:Person)-[:CREATED]->(cand:Series)
        WHERE NOT cand.tmdb_id IN $exclude_ids
        RETURN cand.tmdb_id AS cid, count(DISTINCT p) * $w_creator AS s
        UNION ALL
        WITH src
        MATCH (src)-[:HAS_GENRE]->(:Genre)<-[:HAS_GENRE]-(cand:Series)
        WHERE NOT cand.tmdb_id IN $exclude_ids
        RETURN cand.tmdb_id AS cid, count(*) * $w_genre AS s
        UNION ALL
        WITH src
        MATCH (src)-[:PRODUCED_IN]->(:Country)<-[:PRODUCED_IN]-(cand:Series)
        WHERE NOT cand.tmdb_id IN $exclude_ids
        RETURN cand.tmdb_id AS cid, count(*) * $w_country AS s
    }
    WITH cid, sum(s) AS overlap
    RETURN cid, overlap
    ORDER BY overlap DESC
    LIMIT $need
    """
    params = {
        "source_id": source_id,
        "exclude_ids": list(exclude_ids),
        "need": need,
        "w_actor": GRAPH_W_ACTOR,
        "w_creator": GRAPH_W_CREATOR,
        "w_genre": GRAPH_W_GENRE,
        "w_country": GRAPH_W_COUNTRY,
    }
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, **params)
        rows = await result.data()

    return [(row["cid"], float(row["overlap"])) for row in rows]


async def _hybrid_rerank(
    source_id: int,
    candidate_ids: list[int],
    score_map: dict[int, float],
    exclude_same_creator: bool = False,
    same_country_only: bool = False,
    actor_boost: float = 0.05,
    country_boost: float = 0.1,
    shared: dict[int, tuple[int, int]] | None = None,
) -> tuple[list[int], dict[int, float]]:
    """
    Re-rank кандидатов по графовым признакам.
    Кандидаты, которых нет в графе, отбрасываются (MATCH cand).

    Если передан `shared`, в него пишется {tmdb_id: (shared_actors, shared_countries)} по каждому
    оставшемуся кандидату, чтобы вызывающий код мог вернуть эти числа в ответе.
    """
    cypher = """
    MATCH (src:Series {tmdb_id: $source_id})
    UNWIND $candidate_ids AS cid
    MATCH (cand:Series {tmdb_id: cid})
    OPTIONAL MATCH (src)<-[:ACTED_IN]-(a:Person)-[:ACTED_IN]->(cand)
    WITH cand, cid, count(DISTINCT a) AS shared_actors
    OPTIONAL MATCH (src)-[:PRODUCED_IN]->(c:Country)<-[:PRODUCED_IN]-(cand)
    WITH cand, cid, shared_actors, count(DISTINCT c) AS shared_countries
    OPTIONAL MATCH (src)<-[:CREATED]-(cr:Person)-[:CREATED]->(cand)
    WITH cid, shared_actors, shared_countries, count(DISTINCT cr) AS shared_creators
    RETURN cid, shared_actors, shared_countries, shared_creators
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, source_id=source_id, candidate_ids=candidate_ids)
        rows = await result.data()

    filtered: list[int] = []
    for row in rows:
        cid = row["cid"]

        if exclude_same_creator and row["shared_creators"] > 0:
            continue
        if same_country_only and row["shared_countries"] == 0:
            continue

        boost = min(row["shared_actors"] * actor_boost, actor_boost * 5) + row["shared_countries"] * country_boost
        score_map[cid] = score_map.get(cid, 0.0) + boost
        filtered.append(cid)
        if shared is not None:
            shared[cid] = (row["shared_actors"], row["shared_countries"])

    filtered.sort(key=lambda c: score_map.get(c, 0.0), reverse=True)
    return filtered, score_map


@router.get("/search/persons", response_model=PersonSearchResponse)
async def search_persons(
    q: str = Query(..., min_length=2, max_length=200, description="Имя персоны"),
    limit: int = Query(10, ge=1, le=50),
):
    """
    Полнотекстовый поиск по персонам (актёры, режиссёры, создатели).
    Возвращает список с количеством сериалов — для выбора перед загрузкой графа.
    После выбора персоны фронт вызывает GET /api/person/{person_id}/graph.
    """
    lucene_query = to_lucene_prefix_query(q)
    if not lucene_query:
        return PersonSearchResponse(query=q, results=[], total=0)

    cypher = """
    CALL db.index.fulltext.queryNodes('person_name_idx', $lucene_q)
    YIELD node, score
    WITH node, score, CASE WHEN toLower(node.name) = $q_lc THEN 0 ELSE 1 END AS rank_exact
    OPTIONAL MATCH (node)-[:ACTED_IN|CREATED|DIRECTED]->(s:Series)
    WITH node, score, rank_exact, count(DISTINCT s) AS series_count
    RETURN node.person_id AS person_id,
           node.name      AS name,
           score,
           series_count
    ORDER BY rank_exact, score DESC, series_count DESC
    LIMIT $limit
    """

    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, lucene_q=lucene_query, q_lc=q.strip().lower(), limit=limit)
        rows = await result.data()

    results = [
        PersonResult(
            person_id=r["person_id"],
            name=r["name"] or "",
            score=float(r["score"]),
            series_count=r["series_count"] or 0,
        )
        for r in rows if r.get("person_id") is not None
    ]
    return PersonSearchResponse(query=q, results=results, total=len(results))


async def _structural_search(q: str, limit: int) -> SearchResponse:
    """
    Полнотекстовый поиск по названию через Neo4j-индекс series_name_idx.
    Возвращает сериалы, название которых содержит слова из запроса.
    score — Lucene-релевантность из Neo4j.
    """
    cypher = """
    CALL db.index.fulltext.queryNodes('series_name_idx', $lucene_q)
    YIELD node, score
    // Префиксный запрос даёт почти одинаковый Lucene-score у всех совпадений: сначала точное
    // совпадение названия, затем score, затем популярность как разрешение ничьих.
    WITH node, score,
         CASE WHEN toLower(node.name) = $q_lc
                OR toLower(coalesce(node.original_name, '')) = $q_lc THEN 0 ELSE 1 END AS rank_exact
    ORDER BY rank_exact, score DESC, coalesce(node.popularity, 0) DESC
    LIMIT $limit
    RETURN node.tmdb_id       AS tmdb_id,
           node.name          AS name,
           node.start_year    AS start_year,
           node.end_year      AS end_year,
           node.episode_count AS episode_count,
           node.season_count  AS season_count,
           node.overview      AS overview,
           node.poster_path   AS poster_path,
           node.vote_average  AS vote_average,
           node.vote_count    AS vote_count,
           node.popularity    AS popularity,
           score
    """
    lucene_query = to_lucene_prefix_query(q)
    if not lucene_query:
        return SearchResponse(query=q, mode=SearchMode.structural, results=[], total=0)

    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, lucene_q=lucene_query, q_lc=q.strip().lower(), limit=limit)
        rows = await result.data()

    results = [
        _to_result(r, float(r["score"]), source="graph")
        for r in rows if r.get("tmdb_id") is not None
    ]
    return SearchResponse(query=q, mode=SearchMode.structural, results=results, total=len(results))
