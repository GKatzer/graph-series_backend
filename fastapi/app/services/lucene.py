"""
app/services/lucene.py — сборка безопасных full-text запросов для Neo4j (Lucene-синтаксис).
"""
from __future__ import annotations

import re

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def to_lucene_prefix_query(q: str) -> str:
    """
    Экранирует спецсимволы Lucene в каждом слове запроса и собирает
    AND-запрос с wildcard-суффиксом для prefix-матча.

    Без экранирования спецсимволы пользовательского ввода (например,
    непарная скобка) ломают разбор запроса на стороне Neo4j (500 вместо
    результата поиска) или меняют его смысл.

    Возвращает "" для пустого/пробельного ввода — вызывающий код должен
    в этом случае вернуть пустой результат, не выполняя запрос к Neo4j.
    """
    words = [_LUCENE_SPECIAL.sub(r"\\\1", w) for w in q.split() if w]
    return " AND ".join(f"{w}*" for w in words)
