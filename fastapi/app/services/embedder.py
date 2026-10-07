"""
app/services/embedder.py

VDS1 не держит ML-модель в памяти (4 GB RAM).
Эмбеддинг запроса делается через HTTP-вызов к Qdrant inference
или к отдельному лёгкому эндпоинту на VDS2.

Fallback-вариант (если VDS2 недоступен): синхронный расчёт через
sentence-transformers (добавить в requirements при необходимости).
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger("embedder")

_EMBED_URL = os.getenv(
    "EMBED_SERVICE_URL",
    # По умолчанию — лёгкий inference-эндпоинт на VDS2 (через Tailscale)
    "http://host.docker.internal:8004/embed",
)

_client = httpx.AsyncClient(timeout=10.0)


class EmbeddingUnavailable(RuntimeError):
    """The embedding service did not return a vector (down, timeout, bad answer). Mapped to HTTP 503 in main.py."""


async def embed_text(text: str) -> list[float]:
    """
    Получить эмбеддинг строки.
    Запрос идёт к inference-сервису на VDS2 (bge-small-en-v1.5).

    Документы в Qdrant закодированы без префикса, а запросы inference-сервис
    кодирует с BGE-инструкцией "Represent this sentence for searching relevant
    passages: " (по умолчанию is_query=true) — здесь префикс добавлять не нужно.
    """
    try:
        resp = await _client.post(_EMBED_URL, json={"text": text})
        resp.raise_for_status()
        return resp.json()["vector"]
    except Exception as exc:
        log.error("Embed service error: %s", exc)
        raise EmbeddingUnavailable(f"Cannot get embedding: {exc}") from exc
