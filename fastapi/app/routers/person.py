"""
app/routers/person.py — эндпоинты для персон
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.neo4j import neo4j_driver
from app.schemas import GraphData, GraphEdge, GraphNode, node_id, series_props

router = APIRouter(tags=["person"])


class SeriesRef(BaseModel):
    tmdb_id: int
    name: str
    start_year: int | None
    role: str  # ACTED_IN | CREATED | DIRECTED


class PersonDetail(BaseModel):
    person_id: int
    name: str
    series: list[SeriesRef] = []


@router.get("/person/{person_id}", response_model=PersonDetail)
async def get_person(person_id: int):
    """Данные персоны + фильмография по ролям."""
    cypher = """
    MATCH (p:Person {person_id: $pid})
    OPTIONAL MATCH (p)-[r]->(s:Series)
    WHERE type(r) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
    RETURN p.person_id AS person_id,
           p.name      AS name,
           collect({
               tmdb_id:    s.tmdb_id,
               name:       s.name,
               start_year: s.start_year,
               role:       type(r)
           }) AS series
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, pid=person_id)
        record = await result.single()

    if not record or record["person_id"] is None:
        raise HTTPException(status_code=404, detail=f"Person {person_id} not found")

    series = [
        SeriesRef(**{**s, "name": s["name"] or ""}) for s in record["series"]
        if s.get("tmdb_id") is not None
    ]
    series.sort(key=lambda x: x.start_year or 0, reverse=True)

    return PersonDetail(
        person_id=record["person_id"],
        name=record["name"] or "",
        series=series,
    )


@router.get("/person/{person_id}/graph", response_model=GraphData)
async def person_graph(person_id: int):
    """
    Граф связей персоны — сериалы в которых участвовала,
    и другие персоны через общие сериалы (со-звёзды).
    """
    cypher = """
    MATCH (p:Person {person_id: $pid})-[r]->(s:Series)
    WHERE type(r) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
    WITH p, s, type(r) AS rel
    OPTIONAL MATCH (s)<-[r2]-(co:Person)
    WHERE type(r2) IN ['ACTED_IN', 'CREATED', 'DIRECTED']
      AND co.person_id <> $pid
    WITH p, s, rel, collect(DISTINCT {person: co, rel: type(r2)})[..5] AS costars
    RETURN p,
           collect(DISTINCT {series: s, rel: rel, costars: costars}) AS works
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, pid=person_id)
        record = await result.single()

    if not record:
        raise HTTPException(status_code=404, detail=f"Person {person_id} not found")

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen: set[str] = set()

    def add_node(label: str, key: int | str, name: str, props: dict | None = None) -> str:
        nid = node_id(label, key)
        if nid not in seen:
            seen.add(nid)
            nodes.append(GraphNode(id=nid, key=key, label=label, name=name, properties=props or {}))
        return nid

    p = dict(record["p"])
    pid = add_node("Person", p["person_id"], p.get("name") or str(p["person_id"]))

    for work in record["works"]:
        s = dict(work["series"])
        if s.get("tmdb_id") is None:
            continue
        sid = add_node("Series", s["tmdb_id"], s.get("name") or str(s["tmdb_id"]), series_props(s))
        edges.append(GraphEdge(source=pid, target=sid, type=work["rel"]))

        for costar in work["costars"]:
            co = costar.get("person")
            if not co:
                continue
            co = dict(co)
            if co.get("person_id") is None:
                continue
            cid = add_node("Person", co["person_id"], co.get("name") or str(co["person_id"]))
            edges.append(GraphEdge(source=cid, target=sid, type=costar["rel"]))

    return GraphData(nodes=nodes, edges=edges)
