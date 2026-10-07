"""
app/db/neo4j.py — async Neo4j driver (singleton)
"""
from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import settings


class _Neo4jDriver:
    _driver: AsyncDriver | None = None

    def __init__(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            max_connection_pool_size=20,
            connection_timeout=10,
        )

    async def verify_connectivity(self) -> None:
        await self._driver.verify_connectivity()  # type: ignore[union-attr]

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    async def session(self, **kwargs):  # type: ignore[return]
        return self._driver.session(**kwargs)  # type: ignore[union-attr]


neo4j_driver = _Neo4jDriver()
