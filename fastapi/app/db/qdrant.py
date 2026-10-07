"""
app/db/qdrant.py — Qdrant client (sync, обёрнут в threadpool для async)

Коллекция graph-series: point id = tmdb_id (int), payload
{tmdb_id, name, overview, imdb_id, start_year}. Сервер 1.9.2, только gRPC.
"""
from __future__ import annotations

import asyncio
from functools import partial
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import ScoredPoint

from app.config import settings


class _QdrantClient:
    def __init__(self) -> None:
        self._client = QdrantClient(
            host=settings.qdrant_host,
            port=settings.qdrant_port,
            grpc_port=settings.qdrant_grpc_port,
            prefer_grpc=settings.qdrant_prefer_grpc,
            timeout=10,
        )

    async def verify_connectivity(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._client.get_collections)

    async def search(
        self,
        query_vector: list[float],
        limit: int = 10,
        score_threshold: float = 0.0,
    ) -> list[ScoredPoint]:
        loop = asyncio.get_running_loop()
        fn = partial(
            self._client.search,
            collection_name=settings.qdrant_collection,
            query_vector=query_vector,
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return await loop.run_in_executor(None, fn)

    async def get_by_id(self, tmdb_id: int) -> Any:
        """
        Точка по tmdb_id: point id в коллекции равен tmdb_id, поэтому
        достаём напрямую через retrieve (без scroll по payload).
        """
        loop = asyncio.get_running_loop()
        fn = partial(
            self._client.retrieve,
            collection_name=settings.qdrant_collection,
            ids=[tmdb_id],
            with_payload=True,
            with_vectors=True,
        )
        points = await loop.run_in_executor(None, fn)
        return points[0] if points else None


qdrant_client = _QdrantClient()
