"""
app/main.py — точка входа FastAPI-приложения
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db.neo4j import neo4j_driver
from app.db.qdrant import qdrant_client
from app.routers import graph, health, person, search, series
from app.services.embedder import EmbeddingUnavailable

log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация и teardown при старте/остановке."""
    log.info("Connecting to Neo4j at %s ...", settings.neo4j_uri)
    await neo4j_driver.verify_connectivity()
    log.info("Neo4j OK")

    log.info("Connecting to Qdrant at %s:%s (gRPC) ...", settings.qdrant_host, settings.qdrant_grpc_port)
    await qdrant_client.verify_connectivity()
    log.info("Qdrant OK")

    yield

    log.info("Shutting down — closing connections")
    await neo4j_driver.close()
    # qdrant_client — sync, закрывается автоматически


app = FastAPI(
    title="TV Series Knowledge Graph API",
    version="1.0.0",
    docs_url="/docs" if settings.app_env != "production" else None,
    redoc_url=None,
    lifespan=lifespan,
)

@app.exception_handler(EmbeddingUnavailable)
async def embedding_unavailable(_request: Request, exc: EmbeddingUnavailable) -> JSONResponse:
    """A missing embedding service is a dependency outage (503), not a bug in the request or the app (500)."""
    return JSONResponse(status_code=503, content={"detail": "Embedding service unavailable"})


# ── CORS ──────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)

# ── Роутеры ───────────────────────────────────────────────
app.include_router(health.router)
app.include_router(search.router,  prefix="/api")
app.include_router(graph.router,   prefix="/api")
app.include_router(series.router,  prefix="/api")
app.include_router(person.router,  prefix="/api")