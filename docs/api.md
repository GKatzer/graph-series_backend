# API reference

Every endpoint of the backend, read from the code (`fastapi/app/routers/`). The real, trimmed responses in [`examples/`](examples/) were captured from the public deployment on 2026-10-04 with read-only `GET` requests (regenerate with `docs/examples/fetch_samples.py`) **before** several behaviour changes of the same day (marked *changed* below); once those are deployed, the samples differ in `shared_actors`, `shared_countries`, `source` of structural results and the order of title matches.

**Contents:** [Conventions](#conventions) · [Health](#health) · [Search](#search) · [Series](#series) · [Persons](#persons) · [Graph](#graph) · [Errors](#errors) · [Behaviour worth knowing](#behaviour-worth-knowing)

## Conventions

- Base path: all routes except `/health` and `/health/full` are under `/api`. On the public site the API is reached through the web server's reverse proxy as `/api/backend/…` (the proxy strips the prefix).
- All endpoints are `GET`. CORS allows `GET`, `POST` and `OPTIONS` from the origins in `CORS_ORIGINS_RAW`, without credentials.
- Keys: a series is identified by its TMDB id (`tmdb_id`, integer), a person by `person_id` (integer). Path parameters that must be integers answer **422** otherwise.
- The interactive docs (`/docs`) are served only when `APP_ENV` is not `production`.
- **Graph responses** (`GraphData`) have `nodes` and `edges`. A node is `{ "id": "<label>:<key>", "key": <native key>, "label": …, "name": …, "properties": {…} }`; an edge is `{ "source": <node id>, "target": <node id>, "type": … }`. Native keys of different labels overlap (Series 1399 and Person 1399), so `id` is prefixed with the lower-case label (`series:1399`, `person:1399`, `country:US`). Use `key` when calling other endpoints. For `Country` and `Language` the graph stores only the ISO code, so `name` equals the code.
- Node labels: `Series`, `Person`, `Genre`, `Keyword`, `Network`, `Country`, `Language`. Edge types: `ACTED_IN`, `CREATED`, `DIRECTED` (person → series), `HAS_GENRE`, `HAS_KEYWORD`, `PRODUCED_IN`, `HAS_LANGUAGE`, `AIRED_ON` (series → facet).

## Health

| Method | Path | Response |
|---|---|---|
| GET | `/health` | `{"status": "ok", "uptime_s": <int>}`; used by the Docker health check |
| GET | `/health/full` | *changed:* `{"status": "ok", "neo4j": "up", "qdrant": "up", "fulltext_indexes": "online"}`; **503** with `{"detail": {"errors": {…}}}` naming the failed part: `neo4j`, `qdrant`, or `fulltext_indexes` when `series_name_idx` or `person_name_idx` is missing or not `ONLINE` (with a hint to apply `scripts/schema_init.cypher`) |

```bash
curl -s https://graph-series.katzer.ru/api/backend/health
# {"status":"ok","uptime_s":1146397}
curl -s https://graph-series.katzer.ru/api/backend/health/full
# {"status":"ok","neo4j":"up","qdrant":"up"}      (as deployed on 2026-10-04; the current code also returns "fulltext_indexes")
```

## Search

### `GET /api/search`

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `q` | string, 2 to 500 characters | required | the query |
| `mode` | `semantic` \| `structural` \| `hybrid` | `semantic` | strategy, see below |
| `limit` | 1 to 50 | 10 | number of results |
| `score_threshold` | 0 to 1 | 0.0 | minimum cosine score from Qdrant (`semantic`, `hybrid`; ignored by `structural`) |
| `actor_boost` | 0 to 1 | 0.05 | `hybrid`: bonus per shared actor with the anchor |
| `country_boost` | 0 to 1 | 0.1 | `hybrid`: bonus per shared production country with the anchor |

| Mode | What happens |
|---|---|
| `semantic` | the query is embedded by the inference service; Qdrant returns `limit × 3` nearest vectors; series that are not in the graph are dropped; the first `limit` are enriched from Neo4j and sorted by score |
| `structural` | full-text search on `series_name_idx` (name and original name) with the query turned into an escaped prefix query (`dark ang` becomes `dark* AND ang*`); Qdrant is not used; `score` is Neo4j's full-text relevance. *Changed:* results are ordered by exact title match first, then score, then popularity, because prefix queries give almost equal scores; `source` is `"graph"` |
| `hybrid` | as `semantic`, then the first remaining candidate is the **anchor** and every other candidate gets a bonus for sharing actors (capped) and production countries with the anchor |

Response `SearchResponse`: `{"query", "mode", "results": [SeriesResult…], "total"}`. `total` is the number of returned results (at most `limit`), not the number of matches. A `SeriesResult` has `tmdb_id`, `name`, `start_year`, `end_year`, `episode_count`, `season_count`, `overview`, `poster_path`, `vote_average`, `vote_count`, `popularity`, `score`, `shared_actors`, `shared_countries`, `source`. Poster images are `https://image.tmdb.org/t/p/w500{poster_path}` (TMDB's CDN, not this API).

```bash
curl -s 'https://graph-series.katzer.ru/api/backend/api/search?q=space%20western&mode=semantic&limit=3'
```
```json
{ "query": "space western", "mode": "semantic",
  "results": [
    { "tmdb_id": 30991, "name": "Cowboy Bebop", "start_year": 1998, "end_year": 1999, "episode_count": 26,
      "season_count": 1, "overview": "In 2071, roughly fifty years after …", "poster_path": "/xDiXDfZwC6XYC6fxHI1jl3A3Ill.jpg",
      "vote_average": 8.5, "vote_count": 2026, "popularity": 38.3139,
      "score": 0.7173728942871094, "shared_actors": null, "shared_countries": null, "source": "vector" },
    … ],
  "total": 3 }
```
Full samples: [`search-semantic.json`](examples/search-semantic.json), [`search-hybrid.json`](examples/search-hybrid.json), [`search-structural.json`](examples/search-structural.json).

**Hybrid formula** (`_hybrid_rerank` in `routers/search.py`):

```
score = cosine_score
      + min(shared_actors × actor_boost, 5 × actor_boost)
      + shared_countries × country_boost
```

So with the defaults the actor bonus is at most 0.25, and a score can exceed 1 (the similar example below returned 1.0208 for the top result). The anchor keeps its plain cosine score. *Changed:* every re-ranked result now carries `shared_actors` and `shared_countries` (the counts used for its bonus); the anchor, which has nothing to be compared with, keeps `null`. The deployed samples still show `null` everywhere.

### `GET /api/search/stream`

`q`, `limit`, `score_threshold` as above; semantic search only. A Server-Sent Events stream: `{"type":"meta","total":N}` (an upper bound), then one `{"type":"result","data":{…}}` per series as it is enriched, then `{"type":"done","total":N}`, or `{"type":"error","message":…}`. Series outside the graph are skipped. Sample: [`search-stream.txt`](examples/search-stream.txt).

### `GET /api/search/persons`

`q` (2 to 200 characters), `limit` (1 to 50, default 10). Full-text search on `person_name_idx`, exact name match first, then score, then number of series (*changed*); returns `{"query", "results": [{"person_id", "name", "score", "series_count"}], "total"}`. `series_count` is the number of distinct series linked to the person by any role. Sample: [`search-persons.json`](examples/search-persons.json).

## Series

### `GET /api/series/{tmdb_id}`

The full card, or **404** `{"detail": "Series <id> not found"}`.

| Field | Meaning |
|---|---|
| `tmdb_id`, `name`, `original_name` | ids and titles (`name` as stored; English on the deployment checked) |
| `imdb_id`, `wikidata_id`, `tvdb_id` | cross-references, `null` when absent (the graph stores them as empty strings) |
| `start_year`, `end_year`, `status`, `type`, `in_production`, `original_language` | basic facts (`original_language` is ISO 639-1) |
| `season_count`, `episode_count`, `episode_runtime` | sizes |
| `popularity`, `vote_average`, `vote_count`, `us_content_rating` | TMDB statistics |
| `overview`, `tagline`, `homepage`, `poster_path` | text and links |
| `genres`, `keywords` | names (English on the deployment checked) |
| `countries`, `languages` | ISO 3166-1 / 639-1 codes (all languages of the show) |
| `networks` | `[{network_id, name, country}]` |
| `cast_count`, `creators`, `directors` | number of cast edges; `[{person_id, name}]` |

Sample (trimmed): [`series-1396.json`](examples/series-1396.json). The card is built with pattern comprehensions, one per facet, not a chain of `OPTIONAL MATCH` (see [design decisions](design-decisions.md#cypher-pattern-comprehensions-instead-of-optional-match-chains)).

### `GET /api/series?name=<text>&limit=10`

Autocomplete: full-text search on `series_name_idx`, `name` at least 2 characters, `limit` 1 to 50; exact title match first, then score, then popularity (*changed*). Returns `{"query", "results": [{tmdb_id, name, start_year, end_year, poster_path, score}]}`. Sample: [`series-by-name.json`](examples/series-by-name.json).

### `GET /api/series/{tmdb_id}/similar`

| Parameter | Default | Meaning |
|---|---|---|
| `mode` | `hybrid` | `semantic` or `hybrid` (`structural` is accepted by the enum but behaves like `semantic` without graph re-ranking) |
| `exclude_same_creator` | false | `hybrid`: drop candidates that share a creator with the source |
| `same_country_only` | false | `hybrid`: keep only candidates that share a production country |
| `actor_boost`, `country_boost` | 0.05, 0.1 | as in search |

How it works: the source series' own vector is fetched from Qdrant by point id (`retrieve`, since point id equals `tmdb_id`); its `80` nearest neighbours (4 × the target of 20) are taken without itself; in `hybrid` mode they are re-ranked against the **source** (a known anchor, unlike in `/search`) and series missing from the graph are dropped, otherwise only the graph filter is applied. The list is cut at 20. **If fewer than 10 vector candidates remain** (or the series has no vector), the list is filled up to 20 from the graph by weighted overlap with the source: shared actors × 1.0, shared creators × 3.0, shared genres × 0.1, shared countries × 0.05. In `hybrid` mode the vector entries now carry `shared_actors` and `shared_countries` relative to the source (*changed*). The fill-in entries have `source: "graph"` and their `score` is that weighted count, not a cosine score. There is no `limit` parameter: the response always has at most 20 entries (a `limit` sent by a client is ignored). Sample: [`similar-hybrid.json`](examples/similar-hybrid.json).

## Persons

| Method | Path | Response |
|---|---|---|
| GET | `/api/person/{person_id}` | `{"person_id", "name", "series": [{tmdb_id, name, start_year, role}]}`, newest first; one row per role, so a series appears twice when the person acted in and directed it; **404** when unknown |
| GET | `/api/person/{person_id}/graph` | `GraphData`: the person, their series (edges typed with the role) and up to **5 co-workers per series** (edges typed with their role); **404** when the person has no series |
| GET | `/api/graph/person/{person_id}/series` | `{"person_id", "series": [{tmdb_id, name, start_year, role}]}`, newest first; **404** when empty |

Samples: [`person-17419.json`](examples/person-17419.json).

## Graph

### `GET /api/graph/series/{tmdb_id}`

| Parameter | Default | Meaning |
|---|---|---|
| `depth` | 1 | `1`: the series and its direct neighbours; `2`: additionally up to 50 other series that share people with it, with the shared people |
| `include_types` | all eight edge types | edge types to include; repeat the parameter or separate with commas; unknown names are ignored |

At depth 1 the Breaking Bad graph has 66 nodes and 67 edges on the deployment checked (1 series, 29 persons, 29 keywords, 2 genres, 3 languages, 1 network, 1 country; 20 `ACTED_IN`, 10 `DIRECTED`, 1 `CREATED`, 29 `HAS_KEYWORD`, 2 `HAS_GENRE`, 3 `HAS_LANGUAGE`, 1 `AIRED_ON`, 1 `PRODUCED_IN`): [`graph-series-1396-depth1.counts.json`](examples/graph-series-1396-depth1.counts.json). A filtered request:

```bash
curl -s 'https://graph-series.katzer.ru/api/backend/api/graph/series/1396?depth=1&include_types=HAS_GENRE,PRODUCED_IN'
# nodes: series:1396, genre:80 (Crime), genre:18 (Drama), country:US; edges: HAS_GENRE ×2, PRODUCED_IN ×1
```

Depth 2 adds the shared people with edges typed `ACTED_IN` regardless of their real role in the other series.

### `GET /api/graph/node/{node_type}/{key}`

The graph around a hub: the hub node plus its most popular series. `node_type` is one of `genre`, `keyword`, `network`, `country`, `language`; `key` is the integer id (`genre_id`, `keyword_id`, `network_id`) or the ISO code (`country`, `language`). `limit` 1 to 200, default 50: the series are chosen **inside Neo4j** by `popularity` (descending, then `tmdb_id`), because hubs such as the `en` language or the Drama genre have tens of thousands of series. The hub node carries `properties.total`, its full number of series (23,928 for Drama at capture time). An unknown type gives **400**, a non-integer key for an integer hub **422**, an unknown hub **404**. Sample: [`graph-hub-genre-18.json`](examples/graph-hub-genre-18.json).

### Small structural queries

| Path | Parameters | Response |
|---|---|---|
| `GET /api/graph/series/{tmdb_id}/shared-cast` | `limit` 1 to 100, default 20 | other series by number of shared actors: `{"source", "results": [{tmdb_id, name, start_year, shared_actors}]}` ([sample](examples/graph-shared-cast.json)) |
| `GET /api/graph/intersection` | `series_a`, `series_b` (tmdb ids) | people who took part in both: `{"series_a", "series_b", "intersection": [{person_id, name, role_in_a, role_in_b}]}` ([sample](examples/graph-intersection.json)) |

## Errors

FastAPI's validation errors are returned as `{"detail": [{"type", "loc", "msg", "input", …}]}` with **422**. Application errors use `{"detail": "<text>"}`. Real examples: [`errors.json`](examples/errors.json).

| Case | Status | `detail` |
|---|---|---|
| `q` shorter than 2 characters | 422 | `string_too_short` |
| path id not an integer | 422 | `int_parsing` |
| unknown series or person | 404 | `Series 99999999 not found` |
| unknown hub type (for example `work`) | 400 | `Unknown node type: 'work'. Allowed: [...]` (*changed*: was Russian) |
| non-integer hub key | 422 | `Genre key must be an integer, got 'abc'` (*changed*: was Russian) |
| embedding service unavailable (search, similar) | 503 | `Embedding service unavailable` (*changed*: was a bare 500; the SSE stream sends an `error` event); logged as `Embed service error` |
| database failure | 500 | `Internal Server Error` |
| `/health/full` with a dependency down or an index missing | 503 | `{"errors": {…}}` |

## Behaviour worth knowing

- **The graph is a subset of the vector index** (about 56,000 series with at least two votes against about 211,000 vectors). Search endpoints return only series that exist in the graph, so every result can be opened with the graph endpoints.
- **`source` is `"graph"` for structural results and for the `/similar` fill-in, `"vector"` otherwise** (*changed*; the deployed sample still says `"vector"` for structural results).
- **Structural scores are nearly constant for prefix queries.** For `breaking` three results all had `score` 2.0 on the deployment, so Neo4j's score does not prefer an exact title: *Breaking Bad* came third ([`search-structural.json`](examples/search-structural.json)). The ordering clause added on 2026-10-04 (exact match, then score, then popularity) addresses that; it was verified only with a faked database, not against Neo4j.
- **Searching for a title semantically does not find that title first.** `mode=hybrid` for `breaking bad` returned *Metástasis* (the Colombian remake, whose overview mentions Breaking Bad) first; use `structural` or `/api/series?name=` for titles.
- **Latency varies.** On 2026-10-04 most calls took 1.3 to 4 s from this machine; `shared-cast` took 12.5 s and `intersection` 7.3 s in one try each. These are single requests, not a benchmark.
