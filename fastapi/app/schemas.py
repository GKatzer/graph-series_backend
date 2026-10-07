"""
app/schemas.py — общие модели и хелперы для схемы TMDB
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Ключевое свойство каждого лейбла в Neo4j.
# Series/Person/Genre/Keyword/Network — целочисленные TMDB-id,
# Country/Language — ISO-код (строка, без имени).
NODE_KEY_PROP: dict[str, str] = {
    "Series":   "tmdb_id",
    "Person":   "person_id",
    "Genre":    "genre_id",
    "Keyword":  "keyword_id",
    "Network":  "network_id",
    "Country":  "code",
    "Language": "code",
}


def node_id(label: str, key: int | str) -> str:
    """
    Глобально уникальный id узла для графового ответа.

    Числовые ключи разных лейблов пересекаются (Series 1399 и Person 1399),
    поэтому id префиксуется лейблом: "series:1399", "person:1399", "country:US".
    """
    return f"{label.lower()}:{key}"


def blank_to_none(v: Any) -> Any:
    """Пустые cross-ref'ы хранятся в Neo4j как "" — отдаём null."""
    return None if v == "" else v


def series_props(s: dict) -> dict:
    """Свойства Series-ноды в графовом ответе."""
    return {
        "start_year":   s.get("start_year"),
        "poster_path":  blank_to_none(s.get("poster_path")),
        "vote_average": s.get("vote_average"),
        "popularity":   s.get("popularity"),
    }


class GraphNode(BaseModel):
    id: str              # глобально уникальный: "<label>:<key>"
    key: int | str       # нативный ключ узла (tmdb_id / person_id / … / ISO-код)
    label: str           # Series | Person | Genre | Keyword | Network | Country | Language
    name: str            # у Country/Language — сам ISO-код
    properties: dict = {}


class GraphEdge(BaseModel):
    source: str          # node id
    target: str          # node id
    type: str            # ACTED_IN | CREATED | DIRECTED | HAS_GENRE | HAS_KEYWORD | …


class GraphData(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
