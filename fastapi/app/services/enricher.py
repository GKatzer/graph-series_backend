"""
app/services/enricher.py — обогащение списка tmdb_id данными из Neo4j
"""
from __future__ import annotations

from app.db.neo4j import neo4j_driver


async def enrich_series_list(tmdb_ids: list[int]) -> list[dict]:
    """
    Один батч-запрос к Neo4j для обогащения списка сериалов.
    Возвращает список dict с полями карточки в порядке входного списка.

    Сериалов, которых нет в графе, в ответе нет: в Qdrant лежат все сериалы
    с текстом (~211k), а в Neo4j по умолчанию только vote_count >= 2 (~56k).
    """
    if not tmdb_ids:
        return []

    cypher = """
    UNWIND $ids AS tid
    MATCH (s:Series {tmdb_id: tid})
    RETURN s.tmdb_id       AS tmdb_id,
           s.name          AS name,
           s.start_year    AS start_year,
           s.end_year      AS end_year,
           s.episode_count AS episode_count,
           s.season_count  AS season_count,
           s.overview      AS overview,
           s.poster_path   AS poster_path,
           s.vote_average  AS vote_average,
           s.vote_count    AS vote_count,
           s.popularity    AS popularity
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, ids=tmdb_ids)
        rows = await result.data()

    # Сохраняем порядок из Qdrant
    order = {tid: i for i, tid in enumerate(tmdb_ids)}
    rows.sort(key=lambda r: order.get(r["tmdb_id"], 9999))
    return rows


async def keep_in_graph(tmdb_ids: list[int]) -> list[int]:
    """Оставляет только те tmdb_id, что есть в Neo4j; порядок сохраняется."""
    if not tmdb_ids:
        return []

    cypher = "UNWIND $ids AS tid MATCH (s:Series {tmdb_id: tid}) RETURN tid"
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, ids=tmdb_ids)
        present = {row["tid"] for row in await result.data()}

    return [tid for tid in tmdb_ids if tid in present]
