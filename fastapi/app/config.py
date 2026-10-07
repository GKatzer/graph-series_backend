"""
app/config.py — настройки через pydantic-settings (читает .env / env vars)
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Neo4j
    neo4j_uri: str = "bolt://neo4j:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str

    # Qdrant (VDS2 через Tailscale)
    # Qdrant 1.9.2 слушает только gRPC (:6334), REST (:6333) наружу не торчит.
    qdrant_host: str
    qdrant_port: int = 6333          # REST — не используется, нужен конструктору клиента
    qdrant_grpc_port: int = 6334
    qdrant_prefer_grpc: bool = True
    qdrant_collection: str = "graph-series"

    # App
    app_env: str = "production"
    api_secret_key: str = ""

    # CORS — домены, с которых разрешён доступ к API
    # Несколько значений через запятую: https://a.com,https://b.com
    cors_origins_raw: str = "https://katzer.ru,https://www.katzer.ru,http://localhost:3000"

    @property
    def cors_origins(self) -> list[str]:
        if not self.cors_origins_raw:
            return []
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]
