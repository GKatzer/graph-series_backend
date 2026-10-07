"""
app/routers/graph.py — графовая навигация
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app.db.neo4j import neo4j_driver
from app.schemas import (
    NODE_KEY_PROP,
    GraphData,
    GraphEdge,
    GraphNode,
    node_id,
    series_props,
)

log = logging.getLogger("graph")
router = APIRouter(tags=["graph"])


# ── Граф вокруг сериала ───────────────────────────────────

# Рёбра сериала: тип → (лейбл соседа, входящее ли ребро (сосед → Series), паттерн).
# Паттерны — константы, в Cypher подставляются только ключи этого словаря,
# пользовательский ввод в запрос не попадает.
_SERIES_EDGES: dict[str, tuple[str, bool, str]] = {
    "ACTED_IN":     ("Person",   True,  "(s)<-[:ACTED_IN]-(n:Person)"),
    "CREATED":      ("Person",   True,  "(s)<-[:CREATED]-(n:Person)"),
    "DIRECTED":     ("Person",   True,  "(s)<-[:DIRECTED]-(n:Person)"),
    "HAS_GENRE":    ("Genre",    False, "(s)-[:HAS_GENRE]->(n:Genre)"),
    "HAS_KEYWORD":  ("Keyword",  False, "(s)-[:HAS_KEYWORD]->(n:Keyword)"),
    "PRODUCED_IN":  ("Country",  False, "(s)-[:PRODUCED_IN]->(n:Country)"),
    "HAS_LANGUAGE": ("Language", False, "(s)-[:HAS_LANGUAGE]->(n:Language)"),
    "AIRED_ON":     ("Network",  False, "(s)-[:AIRED_ON]->(n:Network)"),
}


def _node_props(label: str, nd: dict) -> dict:
    """Дополнительные свойства ноды для графового ответа."""
    if label == "Series":
        return series_props(nd)
    if label == "Network":
        return {"country": nd.get("country")}
    return {}


def _node_name(nd: dict, key: int | str) -> str:
    # У Country/Language в графе только ISO-код — имя = код.
    return nd.get("name") or str(key)


@router.get("/graph/series/{tmdb_id}", response_model=GraphData)
async def series_graph(
    tmdb_id: int,
    depth: int = Query(1, ge=1, le=2),
    include_types: list[str] = Query(
        default=list(_SERIES_EDGES),
        description="Типы рёбер: повторяющимся параметром или через запятую",
    ),
):
    allowed = {t.strip() for v in include_types for t in v.split(",") if t.strip()}
    edge_types = [t for t in _SERIES_EDGES if t in allowed]

    # depth=1: прямые связи. Pattern comprehension на каждый тип ребра — иначе
    # цепочка OPTIONAL MATCH перемножает cast × keywords × … в сотни тысяч строк.
    columns = "".join(
        f",\n           [{_SERIES_EDGES[t][2]} | n] AS {t}" for t in edge_types
    )
    cypher_d1 = f"""
    MATCH (s:Series {{tmdb_id: $tid}})
    RETURN s{columns}
    """

    cypher_coappear = """
    MATCH (s:Series {tmdb_id: $tid})
    OPTIONAL MATCH (s)<-[:ACTED_IN|CREATED|DIRECTED]-(p:Person)-[:ACTED_IN|CREATED|DIRECTED]->(s2:Series)
    WHERE s2.tmdb_id <> $tid
    WITH s2, collect(DISTINCT {person: p}) AS shared
    WHERE s2 IS NOT NULL
    WITH s2, shared
    ORDER BY size(shared) DESC
    LIMIT 50
    RETURN collect({series: s2, shared: shared}) AS coappear
    """

    async with await neo4j_driver.session() as session:
        result = await session.run(cypher_d1, tid=tmdb_id)
        record = await result.single()

        coappear: list = []
        if depth == 2 and record:
            result2 = await session.run(cypher_coappear, tid=tmdb_id)
            row2 = await result2.single()
            if row2:
                coappear = row2["coappear"] or []

    if not record:
        raise HTTPException(status_code=404, detail=f"Series {tmdb_id} not found")

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_nodes: set[str] = set()

    def _add_node(label: str, nd: dict) -> str | None:
        key = nd.get(NODE_KEY_PROP[label])
        if key is None:
            return None
        nid = node_id(label, key)
        if nid not in seen_nodes:
            seen_nodes.add(nid)
            nodes.append(GraphNode(
                id=nid, key=key, label=label,
                name=_node_name(nd, key),
                properties=_node_props(label, nd),
            ))
        return nid

    sid = _add_node("Series", dict(record["s"]))
    if sid is None:  # Series nodes are keyed by a constrained tmdb_id, so this is a data error, not a user error
        raise HTTPException(status_code=500, detail=f"Series {tmdb_id} has no key")

    for rel_type in edge_types:
        label, incoming, _ = _SERIES_EDGES[rel_type]
        for node_data in record[rel_type]:
            nid = _add_node(label, dict(node_data))
            if nid is None:
                continue
            if incoming:
                edges.append(GraphEdge(source=nid, target=sid, type=rel_type))
            else:
                edges.append(GraphEdge(source=sid, target=nid, type=rel_type))

    # depth=2: добавляем связанные сериалы через персон
    if depth == 2:
        for entry in coappear:
            s2 = entry.get("series")
            shared = entry.get("shared", [])
            if not s2:
                continue
            s2id = _add_node("Series", dict(s2))
            if s2id is None:
                continue
            # добавляем только общих актёров
            for item in shared:
                p = item.get("person")
                if not p:
                    continue
                pid = _add_node("Person", dict(p))
                if pid is None:
                    continue
                edges.append(GraphEdge(source=pid, target=s2id, type="ACTED_IN"))

    return GraphData(nodes=nodes, edges=edges)


# ── Граф вокруг хаб-ноды (Genre / Keyword / Network / Country / Language) ─────

# Mapping: URL-тип → (Neo4j-лейбл, тип ребра).
# Значения захардкожены — в Cypher нельзя параметризовать лейблы/типы рёбер.
_HUB_CONFIG: dict[str, tuple[str, str]] = {
    "genre":    ("Genre",    "HAS_GENRE"),
    "keyword":  ("Keyword",  "HAS_KEYWORD"),
    "network":  ("Network",  "AIRED_ON"),
    "country":  ("Country",  "PRODUCED_IN"),
    "language": ("Language", "HAS_LANGUAGE"),
}


@router.get("/graph/node/{node_type}/{key}", response_model=GraphData)
async def hub_node_graph(
    node_type: str,
    key: str,
    limit: int = Query(50, ge=1, le=200, description="Макс. число сериалов в ответе"),
):
    """
    Граф вокруг узла-хаба: Genre, Keyword, Network, Country или Language.
    Возвращает сам хаб + самые популярные ссылающиеся на него сериалы.
    Вызывается по двойному клику по таким нодам в UI.

    `key` — genre_id / keyword_id / network_id (целое) либо ISO-код
    для country / language.
    """
    if node_type not in _HUB_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown node type: {node_type!r}. Allowed: {sorted(_HUB_CONFIG)}",
        )

    neo4j_label, rel_type = _HUB_CONFIG[node_type]
    key_prop = NODE_KEY_PROP[neo4j_label]

    hub_key: int | str = key
    if key_prop != "code":
        try:
            hub_key = int(key)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{neo4j_label} key must be an integer, got {key!r}",
            )

    # Лейбл, ключ и тип ребра берём из доверенных словарей, поэтому f-string безопасен.
    # Топ-N по популярности отбирается внутри Neo4j: у хабов вроде Language en / Genre
    # Drama десятки тысяч сериалов, собирать их все в список нельзя.
    cypher = f"""
    MATCH (hub:{neo4j_label} {{{key_prop}: $key}})
    CALL {{
        WITH hub
        OPTIONAL MATCH (s:Series)-[:{rel_type}]->(hub)
        RETURN s
        ORDER BY coalesce(s.popularity, 0) DESC, s.tmdb_id
        LIMIT $limit
    }}
    RETURN hub,
           COUNT {{ (:Series)-[:{rel_type}]->(hub) }} AS total,
           collect(s) AS series_list
    """

    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, key=hub_key, limit=limit)
        record = await result.single()

    if not record:
        raise HTTPException(
            status_code=404,
            detail=f"{neo4j_label} {key} not found",
        )

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []

    hub = dict(record["hub"])
    hub_id = node_id(neo4j_label, hub_key)
    nodes.append(GraphNode(
        id=hub_id, key=hub_key, label=neo4j_label,
        name=_node_name(hub, hub_key),
        properties={**_node_props(neo4j_label, hub), "total": record["total"]},
    ))

    for s in record["series_list"]:
        if not s:
            continue
        sd = dict(s)
        if sd.get("tmdb_id") is None:
            continue
        sid = node_id("Series", sd["tmdb_id"])
        nodes.append(GraphNode(
            id=sid, key=sd["tmdb_id"], label="Series",
            name=sd.get("name") or str(sd["tmdb_id"]),
            properties=series_props(sd),
        ))
        edges.append(GraphEdge(source=sid, target=hub_id, type=rel_type))

    return GraphData(nodes=nodes, edges=edges)


# ── Сериалы актёра ────────────────────────────────────────

@router.get("/graph/person/{person_id}/series")
async def person_series(person_id: int):
    """Все сериалы, в которых участвовала персона (актёр/создатель/режиссёр)."""
    cypher = """
    MATCH (p:Person {person_id: $pid})-[r]->(s:Series)
    WHERE type(r) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
    RETURN s.tmdb_id     AS tmdb_id,
           s.name        AS name,
           s.start_year  AS start_year,
           type(r)       AS role
    ORDER BY s.start_year DESC
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, pid=person_id)
        rows = await result.data()

    if not rows:
        raise HTTPException(status_code=404, detail=f"Person {person_id} not found")
    return {"person_id": person_id, "series": rows}


# ── Пересечение каста двух сериалов ──────────────────────

@router.get("/graph/intersection")
async def cast_intersection(
    series_a: int = Query(..., description="tmdb_id первого сериала"),
    series_b: int = Query(..., description="tmdb_id второго сериала"),
):
    """
    Персоны, которые участвовали в обоих сериалах
    (любая роль: актёр, создатель, режиссёр).
    """
    cypher = """
    MATCH (a:Series {tmdb_id: $a})<-[r1]-(p:Person)-[r2]->(b:Series {tmdb_id: $b})
    WHERE type(r1) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
      AND type(r2) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
    RETURN p.person_id AS person_id,
           p.name      AS name,
           type(r1)    AS role_in_a,
           type(r2)    AS role_in_b
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, a=series_a, b=series_b)
        rows = await result.data()
    return {"series_a": series_a, "series_b": series_b, "intersection": rows}


# ── Структурный запрос: общие актёры ─────────────────────

@router.get("/graph/series/{tmdb_id}/shared-cast")
async def shared_cast(
    tmdb_id: int,
    limit: int = Query(20, ge=1, le=100),
):
    """
    Другие сериалы, отсортированные по количеству общих актёров.
    """
    cypher = """
    MATCH (s:Series {tmdb_id: $tid})<-[:ACTED_IN]-(a:Person)-[:ACTED_IN]->(other:Series)
    WHERE other.tmdb_id <> $tid
    RETURN other.tmdb_id      AS tmdb_id,
           other.name         AS name,
           other.start_year   AS start_year,
           count(DISTINCT a)  AS shared_actors
    ORDER BY shared_actors DESC
    LIMIT $limit
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, tid=tmdb_id, limit=limit)
        rows = await result.data()
    return {"source": tmdb_id, "results": rows}
