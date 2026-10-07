// =============================================================
//  schema_init.cypher — создание индексов и constraints Neo4j
//  (схема TMDB: ключ Series = tmdb_id)
//
//  Идемпотентно (IF NOT EXISTS). Запускать после первого старта Neo4j
//  и ПОВТОРНО после каждой полной очистки и перезагрузки графа (репо ETL,
//  `neo4j_loader.py`): очистка сносит схему (constraints + indexes), а loader
//  её не создаёт, включая full-text индексы, на которых держатся /api/series
//  и /api/search (series_name_idx, person_name_idx).
//
//  docker compose exec -T neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}"' \
//    < scripts/schema_init.cypher
// =============================================================

// ── Unique constraints (автоматически создают индекс) ────────
// Series/Person/Genre/Keyword/Network — целочисленные TMDB-id,
// Country/Language — ISO-код (3166-1 alpha-2 / 639-1).

CREATE CONSTRAINT series_tmdb_id IF NOT EXISTS
  FOR (s:Series) REQUIRE s.tmdb_id IS UNIQUE;

CREATE CONSTRAINT person_person_id IF NOT EXISTS
  FOR (p:Person) REQUIRE p.person_id IS UNIQUE;

CREATE CONSTRAINT genre_genre_id IF NOT EXISTS
  FOR (g:Genre) REQUIRE g.genre_id IS UNIQUE;

CREATE CONSTRAINT keyword_keyword_id IF NOT EXISTS
  FOR (k:Keyword) REQUIRE k.keyword_id IS UNIQUE;

CREATE CONSTRAINT network_network_id IF NOT EXISTS
  FOR (n:Network) REQUIRE n.network_id IS UNIQUE;

CREATE CONSTRAINT country_code IF NOT EXISTS
  FOR (c:Country) REQUIRE c.code IS UNIQUE;

CREATE CONSTRAINT language_code IF NOT EXISTS
  FOR (l:Language) REQUIRE l.code IS UNIQUE;

// ── Full-text индексы для поиска по подстроке ────────────────

// name в TMDB локализован (ru), original_name — оригинальное название:
// без него "Game of Thrones" не находит сериал с name = "Игра Престолов".
// Определение полнотекстового индекса на месте не меняется. Если series_name_idx уже
// создан по одному name — сначала: DROP INDEX series_name_idx IF EXISTS;
CREATE FULLTEXT INDEX series_name_idx IF NOT EXISTS
  FOR (s:Series) ON EACH [s.name, s.original_name];

CREATE FULLTEXT INDEX person_name_idx IF NOT EXISTS
  FOR (p:Person) ON EACH [p.name];

// ── Дополнительные индексы для частых запросов ───────────────

// Поиск сериалов по году начала (фильтр в гибридном поиске)
CREATE INDEX series_start_year IF NOT EXISTS
  FOR (s:Series) ON (s.start_year);

// ── Вывод текущих индексов для проверки ──────────────────────
SHOW INDEXES;
