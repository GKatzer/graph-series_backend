"""
scripts/eval_hybrid.py
~~~~~~~~~~~~~~~~~~~~~~
Проверяет, помогает ли hybrid re-rank (`_hybrid_rerank` в app/routers/search.py)
находить более релевантные похожие сериалы, или просто переставляет тот же
список semantic с шумом сверху.

Ground truth -- НЕ shared_actors/shared_countries: это ровно то, что сам
hybrid-буст оптимизирует, и мерить его по собственному сигналу значило бы
отвечать самому себе (тот же принцип, что и в Pinance -- не берём метрику
успеха из того, что уже участвует в кандидате). Жанр в _hybrid_rerank не
участвует вообще (GRAPH_W_GENRE есть только в отдельном graph-фолбэке
/similar, не здесь), поэтому genre overlap@k -- независимый сигнал.

Сравниваются три варианта поверх ОДНИХ И ТЕХ ЖЕ векторных кандидатов
(overfetch из Qdrant), настоящей функцией бэкенда _hybrid_rerank, а не её
переписанной копией -- чтобы результат эвала не разошёлся с продовым кодом
так, как когда-то разошлись train- и serve-версии признака в Pinance:
  - semantic       -- порядок Qdrant, только keep_in_graph
  - hybrid         -- _hybrid_rerank с боевыми весами (--actor-boost/--country-boost)
  - hybrid (нулевые веса) -- тот же код-путь, буст занулён; отделяет эффект
    самого прохода через _hybrid_rerank (фильтрация по графу и т.п.) от
    эффекта самого буста

Метрики: genre_overlap@k для каждого варианта (среднее по source), churn@k
(доля топ-k, отличающаяся от semantic) и parity-bootstrap CI для дельт
hybrid-semantic и hybrid-noboost -- тот же приём, что и Pinance-репорты
(paired-разница + bootstrap), только пересчитанный без pandas/numpy: бэкенд
сознательно не тянет ML-зависимости ради разового скрипта (см. requirements.txt).

Источник выборки -- случайные tmdb_id прямо из Neo4j (`ORDER BY rand()`),
не датасет ETL: тут оценивается сервинг, а не эмбеддинги, поэтому нужны
именно те сериалы, что сейчас есть в графе. Требует живые Neo4j + Qdrant
(тот же .env, что и сам API) -- запускать с машины, у которой есть доступ
к обоим (VDS1 сам, или desktop/VDS3 через Tailscale).

Вне охвата: GRAPH_W_ACTOR/CREATOR/GENRE/COUNTRY (search.py, _graph_fallback) --
отдельная функция, включается только когда векторных кандидатов < 10 в
/similar. Этот скрипт всегда берёт k*8 кандидатов из Qdrant, так что тонкое
покрытие почти никогда не воспроизводится случайной выборкой -- эти веса
им не проверены. Нужна отдельная выборка сериалов с малым покрытием в
Qdrant, это не сделано.

Запуск (из fastapi/, либо из корня репозитория как `python fastapi/scripts/eval_hybrid.py`
-- sys.path чинится от __file__, а не от cwd):
  cd fastapi
  python scripts/eval_hybrid.py                          # 200 сериалов, top-10, боевые веса
  python scripts/eval_hybrid.py --n 500 --k 20
  python scripts/eval_hybrid.py --actor-boost 0.1 --country-boost 0.2
  python scripts/eval_hybrid.py --verbose                # примеры, где hybrid хуже semantic
  python scripts/eval_hybrid.py --csv report.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv as csv_module
import logging
import random
import statistics
import sys
import time
from pathlib import Path

# `python scripts/eval_hybrid.py` puts scripts/ on sys.path[0], not fastapi/
# (unlike pytest, which inserts the rootdir for tests/) — без этой строки
# `from app...` падает с ModuleNotFoundError независимо от cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.neo4j import neo4j_driver
from app.db.qdrant import qdrant_client
from app.routers.search import _hybrid_rerank
from app.services.enricher import keep_in_graph

log = logging.getLogger("eval_hybrid")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")

DEFAULT_N = 200
DEFAULT_K = 10
DEFAULT_ACTOR_BOOST = 0.05
DEFAULT_COUNTRY_BOOST = 0.1
OVERFETCH = 8  # кандидатов из Qdrant на один source = k * OVERFETCH, как в similar_series


# ---------------------------------------------------------------------------
# Выборка source-сериалов и жанры -- прямые Cypher-запросы, в обход HTTP-слоя
# ---------------------------------------------------------------------------
async def sample_source_ids(n: int) -> list[int]:
    """n случайных tmdb_id сериалов, у которых есть хотя бы один жанр
    (иначе genre_overlap для них не определить).

    ORDER BY rand() -- полный скан лейбла Series, это ок для разового
    ручного запуска (эта же оговорка в graph.py про coappear-подзапрос),
    но не паттерн для прод-эндпоинта.
    """
    cypher = """
    MATCH (s:Series)
    WHERE EXISTS { (s)-[:HAS_GENRE]->() }
    WITH s, rand() AS r
    ORDER BY r
    LIMIT $n
    RETURN s.tmdb_id AS tmdb_id
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, n=n)
        rows = await result.data()
    return [row["tmdb_id"] for row in rows]


async def fetch_genres(tmdb_ids: list[int]) -> dict[int, set[int]]:
    """{tmdb_id: {genre_id, ...}} для батча сериалов, включая те, у кого
    жанров нет (пустой set) -- одним запросом, как enrich_series_list."""
    if not tmdb_ids:
        return {}
    cypher = """
    UNWIND $ids AS tid
    MATCH (s:Series {tmdb_id: tid})
    RETURN tid, [(s)-[:HAS_GENRE]->(g:Genre) | g.genre_id] AS genre_ids
    """
    async with await neo4j_driver.session() as session:
        result = await session.run(cypher, ids=list(set(tmdb_ids)))
        rows = await result.data()
    return {row["tid"]: set(row["genre_ids"]) for row in rows}


# ---------------------------------------------------------------------------
# Три варианта ранжирования поверх одних и тех же векторных кандидатов
# ---------------------------------------------------------------------------
async def rank_variants(
    source_id: int, k: int, actor_boost: float, country_boost: float
) -> dict[str, list[int]] | None:
    """None -- у сериала нет вектора в Qdrant (не проиндексирован), source пропускается."""
    point = await qdrant_client.get_by_id(source_id)
    if not point or not point.vector:
        return None

    hits = await qdrant_client.search(point.vector, limit=k * OVERFETCH)
    hits = [h for h in hits if int(h.id) != source_id]
    if not hits:
        return None

    candidate_ids = [int(h.id) for h in hits]
    base_scores = {int(h.id): h.score for h in hits}

    semantic_ids = (await keep_in_graph(candidate_ids))[:k]

    hybrid_ids, _ = await _hybrid_rerank(
        source_id=source_id, candidate_ids=candidate_ids, score_map=dict(base_scores),
        actor_boost=actor_boost, country_boost=country_boost,
    )
    noboost_ids, _ = await _hybrid_rerank(
        source_id=source_id, candidate_ids=candidate_ids, score_map=dict(base_scores),
        actor_boost=0.0, country_boost=0.0,
    )
    return {"semantic": semantic_ids, "hybrid": hybrid_ids[:k], "noboost": noboost_ids[:k]}


def genre_overlap_at_k(result_ids: list[int], src_genres: set[int], genres: dict[int, set[int]]) -> float:
    if not result_ids:
        return 0.0
    return sum(1 for rid in result_ids if genres.get(rid, set()) & src_genres) / len(result_ids)


def churn_at_k(a: list[int], b: list[int]) -> float:
    """Доля b, которой нет в a -- насколько сильно re-rank поменял топ-k."""
    if not b:
        return 0.0
    a_set = set(a)
    return sum(1 for x in b if x not in a_set) / len(b)


# ---------------------------------------------------------------------------
# Paired bootstrap для дельты среднего (без numpy/pandas -- см. докстринг файла)
# ---------------------------------------------------------------------------
def bootstrap_delta_ci(a: list[float], b: list[float], n_boot: int, seed: int) -> tuple[float, float, float]:
    """b - a, попарно по одному и тому же source. Возвращает (mean_delta, ci_lo, ci_hi)."""
    n = len(a)
    deltas = [b[i] - a[i] for i in range(n)]
    mean_delta = statistics.mean(deltas) if deltas else 0.0
    if n < 2:
        return mean_delta, mean_delta, mean_delta

    rng = random.Random(seed)
    idx = range(n)
    boots = [statistics.mean(deltas[i] for i in rng.choices(idx, k=n)) for _ in range(n_boot)]
    boots.sort()
    lo = boots[int(0.025 * n_boot)]
    hi = boots[int(0.975 * n_boot) - 1]
    return mean_delta, lo, hi


# ---------------------------------------------------------------------------
# Отчёт -- тот же стиль, что validate.py / eval_retrieval.py (ETL)
# ---------------------------------------------------------------------------
RESET, GREEN, YELLOW, BOLD, CYAN = "\033[0m", "\033[32m", "\033[33m", "\033[1m", "\033[36m"


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


def print_report(rows: list[dict], k: int, n_boot: int, seed: int, verbose: bool) -> None:
    n = len(rows)
    sem = [r["semantic_overlap"] for r in rows]
    hyb = [r["hybrid_overlap"] for r in rows]
    nob = [r["noboost_overlap"] for r in rows]
    churn = [r["churn"] for r in rows]

    d_hyb_sem = bootstrap_delta_ci(sem, hyb, n_boot, seed)
    d_hyb_nob = bootstrap_delta_ci(nob, hyb, n_boot, seed)

    print()
    print(_c("═" * 64, BOLD))
    print(_c("  Hybrid Re-rank Eval Report (genre overlap@k, независимый сигнал)", BOLD))
    print(_c("═" * 64, BOLD))
    print(f"\n  сериалов в выборке: {n}  |  top-k: {k}\n")
    print(f"  {_c('genre_overlap@k  semantic', CYAN)}:          {statistics.mean(sem):.3f}")
    print(f"  {_c('genre_overlap@k  hybrid (боевые веса)', CYAN)}: {statistics.mean(hyb):.3f}")
    print(f"  {_c('genre_overlap@k  hybrid (нулевые веса)', CYAN)}: {statistics.mean(nob):.3f}")
    print(f"  {_c('churn@k  (hybrid отличается от semantic)', CYAN)}: {statistics.mean(churn):.3f}")
    print()
    print(f"  Δ hybrid − semantic: {d_hyb_sem[0]:+.3f}  "
          f"95% CI [{d_hyb_sem[1]:+.3f}, {d_hyb_sem[2]:+.3f}]"
          f"  {_c('(доверительный интервал не пересекает 0 -> эффект есть)', YELLOW) if d_hyb_sem[1] > 0 or d_hyb_sem[2] < 0 else ''}")
    print(f"  Δ hybrid − noboost:  {d_hyb_nob[0]:+.3f}  "
          f"95% CI [{d_hyb_nob[1]:+.3f}, {d_hyb_nob[2]:+.3f}]")

    if verbose:
        worst = sorted(rows, key=lambda r: r["hybrid_overlap"] - r["semantic_overlap"])[:10]
        print(f"\n{_c('10 сериалов, где hybrid просел сильнее всего относительно semantic:', YELLOW)}")
        for r in worst:
            print(f"    tmdb_id={r['tmdb_id']}  semantic={r['semantic_overlap']:.2f}  "
                  f"hybrid={r['hybrid_overlap']:.2f}  churn={r['churn']:.2f}")

    print()
    print(_c("─" * 64, BOLD))
    print()


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------
async def run(n: int, k: int, actor_boost: float, country_boost: float,
              n_boot: int, seed: int, verbose: bool, csv_path: Path | None) -> None:
    await neo4j_driver.verify_connectivity()
    await qdrant_client.verify_connectivity()

    log.info("Выбираем %d случайных сериалов с жанрами из Neo4j...", n)
    source_ids = await sample_source_ids(n)
    log.info("Получено %d source-сериалов", len(source_ids))

    t0 = time.time()
    rows: list[dict] = []
    skipped = 0
    for i, source_id in enumerate(source_ids, start=1):
        variants = await rank_variants(source_id, k, actor_boost, country_boost)
        if variants is None:
            skipped += 1
            continue

        all_ids = {source_id, *variants["semantic"], *variants["hybrid"], *variants["noboost"]}
        genres = await fetch_genres(list(all_ids))
        src_genres = genres.get(source_id, set())
        if not src_genres:
            skipped += 1
            continue

        rows.append({
            "tmdb_id": source_id,
            "semantic_overlap": genre_overlap_at_k(variants["semantic"], src_genres, genres),
            "hybrid_overlap": genre_overlap_at_k(variants["hybrid"], src_genres, genres),
            "noboost_overlap": genre_overlap_at_k(variants["noboost"], src_genres, genres),
            "churn": churn_at_k(variants["semantic"], variants["hybrid"]),
        })

        if i % 50 == 0:
            log.info("  прогресс: %d / %d", i, len(source_ids))

    elapsed = time.time() - t0
    log.info("✔ Готово за %.1f сек, пропущено %d (нет вектора/жанра)", elapsed, skipped)

    if not rows:
        log.error("Не набралось ни одного валидного source -- отчёт пуст.")
        raise SystemExit(1)

    print_report(rows, k, n_boot, seed, verbose)

    if csv_path:
        with csv_path.open("w", newline="") as f:
            writer = csv_module.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        log.info("✔ Построчный отчёт сохранён: %s", csv_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Оценка hybrid re-rank независимым сигналом (genre overlap)")
    parser.add_argument("--n", type=int, default=DEFAULT_N, help=f"Размер выборки source-сериалов (default {DEFAULT_N})")
    parser.add_argument("--k", type=int, default=DEFAULT_K, help=f"Глубина топ-k (default {DEFAULT_K})")
    parser.add_argument("--actor-boost", type=float, default=DEFAULT_ACTOR_BOOST)
    parser.add_argument("--country-boost", type=float, default=DEFAULT_COUNTRY_BOOST)
    parser.add_argument("--n-boot", type=int, default=5000, help="Число bootstrap-повторов для CI")
    parser.add_argument("--seed", type=int, default=42, help="Seed для bootstrap (сама выборка Neo4j не сидируется -- rand() без APOC)")
    parser.add_argument("--verbose", action="store_true", help="Показать сериалы, где hybrid просел сильнее всего")
    parser.add_argument("--csv", type=Path, default=None, help="Сохранить построчный отчёт в CSV")
    args = parser.parse_args()

    asyncio.run(run(
        n=args.n, k=args.k,
        actor_boost=args.actor_boost, country_boost=args.country_boost,
        n_boot=args.n_boot, seed=args.seed,
        verbose=args.verbose, csv_path=args.csv,
    ))


if __name__ == "__main__":
    main()
