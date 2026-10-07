"""
app/routers/health.py
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException

from app.db.neo4j import neo4j_driver
from app.db.qdrant import qdrant_client

router = APIRouter(tags=["health"])
REQUIRED_FULLTEXT_INDEXES = ("series_name_idx", "person_name_idx")
_start_time = time.time()


@router.get("/health")
async def health():
    """Базовый healthcheck — используется Caddy и Docker."""
    return {"status": "ok", "uptime_s": round(time.time() - _start_time)}


@router.get("/health/full")
async def health_full():
    """Полная проверка зависимостей: Neo4j, Qdrant и full-text индексы Neo4j (должны быть ONLINE)."""
    errors: dict[str, str] = {}

    # Neo4j
    try:
        async with await neo4j_driver.session() as s:
            await s.run("RETURN 1")
    except Exception as exc:
        errors["neo4j"] = str(exc)

    # Full-text indexes: structural search, person search and /api/series?name= fail with HTTP 500
    # when they are missing (they are lost on a graph wipe and are not recreated by the loader).
    try:
        async with await neo4j_driver.session() as s:
            result = await s.run(
                "SHOW INDEXES YIELD name, type, state WHERE type = 'FULLTEXT' RETURN name, state"
            )
            states = {row["name"]: row["state"] for row in await result.data()}
        bad = {n: states.get(n, "MISSING") for n in REQUIRED_FULLTEXT_INDEXES if states.get(n) != "ONLINE"}
        if bad:
            errors["fulltext_indexes"] = f"not ONLINE: {bad}; apply scripts/schema_init.cypher"
    except Exception as exc:
        errors["fulltext_indexes"] = str(exc)

    # Qdrant
    try:
        await qdrant_client.verify_connectivity()
    except Exception as exc:
        errors["qdrant"] = str(exc)

    if errors:
        raise HTTPException(status_code=503, detail={"errors": errors})

    return {"status": "ok", "neo4j": "up", "qdrant": "up", "fulltext_indexes": "online"}
