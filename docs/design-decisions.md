# Design decisions

The decisions that shape the backend, with the reason where the code or its comments give one. Where the code does not say why, the entry says what the choice buys and is marked *(inferred from the code)*. Nothing here is taken from old design documents.

**Contents:** [Two stores, one key](#two-stores-one-key-and-the-graph-is-a-subset) · [Embeddings come from a separate service](#embeddings-come-from-a-separate-service) · [Over-fetch and filter](#over-fetch-from-qdrant-then-filter-to-the-graph) · [Hybrid ranking](#hybrid-ranking-an-anchor-and-two-capped-bonuses) · [Graph fill-in for similar](#graph-fill-in-when-the-vector-index-has-too-few-neighbours) · [Pattern comprehensions](#cypher-pattern-comprehensions-instead-of-optional-match-chains) · [Top-N inside Neo4j](#top-n-is-chosen-inside-neo4j) · [Query safety](#query-safety-parameters-whitelists-and-lucene-escaping) · [Exact titles first](#structural-search-puts-exact-titles-first) · [Health check covers indexes](#the-health-check-also-checks-the-indexes) · [Node ids](#node-ids-are-labelkey) · [Qdrant client pinned](#qdrant-client-pinned-to-the-server-version) · [Sync client in a thread pool](#qdrant-sync-client-in-a-thread-pool) · [Deployment choices](#deployment-choices) · [Evaluation script](#the-evaluation-calls-production-code) · [Changed or dropped](#changed-or-dropped-along-the-way)

## Two stores, one key, and the graph is a subset

Neo4j answers structural questions (who acted in what, which genres and keywords a series has); Qdrant answers "what feels like this". Both are keyed by TMDB's `tmdb_id`: it is the Qdrant point id and the Neo4j node key, so a vector hit becomes a graph node without a lookup table. The vector index holds every series that has a text (about 211,000), the graph only series with at least two votes (about 56,000). The API therefore returns only series that exist in the graph, which guarantees that each result can be opened as a graph. *(The ratio and the vote threshold are stated in the code comments and the Methodology page of the web app; the loading code is in `graph-series_ETL`.)*

## Embeddings come from a separate service

The API server has 4 GB of RAM, of which Neo4j takes about 1.5 GB. Loading `sentence-transformers` and its weights into the API process would eat the rest, so `services/embedder.py` posts the query to an inference service on the other server (10 s timeout). The service adds the BGE retrieval instruction to queries only, which is how the model is meant to be used and matches how the documents were embedded (without it); the backend must not add the prefix itself (stated in the function's docstring). The address comes from `EMBED_SERVICE_URL`. An embedding failure is raised as `EmbeddingUnavailable` and mapped to HTTP 503 `Embedding service unavailable` (a dependency outage, not an application bug); it used to surface as a bare 500.

## Over-fetch from Qdrant, then filter to the graph

Because the graph is a subset, a plain top-`limit` from Qdrant can lose most of its hits. `/api/search` asks Qdrant for `limit × 3` (`GRAPH_COVERAGE_OVERFETCH`) and keeps the ones present in Neo4j; `/similar` asks for 80. One batch query (`keep_in_graph`) does the filtering, and one more (`enrich_series_list`, `UNWIND` over the ids) builds the cards in the order Qdrant returned them.

## Hybrid ranking: an anchor and two capped bonuses

`/search` has no source series, so `hybrid` takes the first candidate that is in the graph as the **anchor** and scores every other candidate by what it shares with the anchor: a bonus per shared actor (capped at five actors, so a huge ensemble cannot dominate) and a bonus per shared production country, both added to the cosine score. The weights `actor_boost = 0.05` and `country_boost = 0.1` were chosen by hand and are request parameters. Consequences, stated plainly:

- the bonus measures closeness to the anchor, not to the meaning of the query, so a wrong anchor drags the list;
- a score can exceed 1;
- the shared counts used for the bonus are returned as `shared_actors` and `shared_countries` (they used to be dropped, which left the fields `null` and the explanation the web app shows empty); the anchor itself has none.

`/similar` is the better case: the anchor is the series the user asked about. How much the bonuses help is measured in [evaluation.md](evaluation.md): a small but statistically distinguishable gain, at the price of changing a fifth of the top 10.

## Graph fill-in when the vector index has too few neighbours

If fewer than 10 vector candidates remain for `/similar` (the series is rare, or has no vector at all), the list is topped up to 20 from the graph: candidates are ranked by weighted overlap with the source, with actors 1.0, creators 3.0, genres 0.1 and countries 0.05 (`GRAPH_W_*`; shared creators weigh most, a shared genre almost nothing). The aggregation runs inside Neo4j and is cut with `LIMIT`, so a large cast never has to be unfolded in application memory (comment in `_graph_fallback`). This guarantees a non-empty answer for any series that is in the graph. These weights are not covered by the evaluation.

## Cypher pattern comprehensions instead of `OPTIONAL MATCH` chains

A series card needs genres, keywords, countries, languages, networks, creators and directors. A chain of `OPTIONAL MATCH` clauses multiplies the rows (cast × keywords × directors × …) into hundreds of thousands of rows for one series, so `get_series` and `series_graph` use one pattern comprehension per facet (`[(s)-[:HAS_GENRE]->(g) | g.name]`) and `COUNT { … }` for the cast size (comments in `series.py` and `graph.py`).

## Top-N is chosen inside Neo4j

Hub nodes such as the language `en` or the genre Drama are linked to tens of thousands of series. `hub_node_graph` sorts by `popularity` and applies `LIMIT` in a `CALL { … }` subquery, and reports the full count as `properties.total` through a `COUNT { … }` expression, instead of collecting every series and trimming in Python.

## Query safety: parameters, whitelists and Lucene escaping

- Values always reach Cypher as parameters. The few places where a label or relationship type has to be written into the query (Cypher cannot parameterise them) take it from constant dictionaries (`_SERIES_EDGES`, `_HUB_CONFIG`, `NODE_KEY_PROP`), never from the request; an unknown hub type is rejected with 400 and unknown `include_types` names are ignored.
- Full-text input goes through `to_lucene_prefix_query`. Without escaping, a stray bracket or quote in the user's text made Neo4j fail to parse the query (a 500 instead of results) or changed its meaning (comment in `services/lucene.py`). Every special character is escaped, each word gets a `*` suffix for prefix matching, and the words are joined with `AND`.
- The OpenAPI docs are switched off when `APP_ENV=production`.

## Structural search puts exact titles first

A prefix query (`breaking*`) makes Lucene give nearly the same score to every match, so the order was arbitrary and *Breaking Bad* was third for `breaking`. Title search (`/api/search?mode=structural`, `/api/series?name=`) now orders by exact name match (name or original name), then score, then popularity; person search by exact name, score, then number of series. Ties are broken by something a user expects instead of by index order. The clauses use `WITH … ORDER BY … LIMIT` before the final `RETURN`; they were unit-tested with a faked database but not executed against Neo4j.

## The health check also checks the indexes

On 2026-10-04 two search modes answered 500 for hours because the full-text indexes were gone after a graph reload, and `/health/full` only checked connectivity. It now runs `SHOW INDEXES` and reports 503 unless both indexes are `ONLINE`, which also covers the `POPULATING` window after (re)creation. The Docker health check still uses the cheap `/health`.

## Node ids are `label:key`

TMDB keys of different labels overlap (Series 1399 and Person 1399), so graph responses carry `id = "<label>:<key>"` next to the native `key`; the client uses the prefixed id for identity and the key for further calls. Earlier versions used Wikidata ids; the move to TMDB (see the schema file header) replaced them. Details: [api.md](api.md#conventions).

## Qdrant client pinned to the server version

`requirements.txt` pins `qdrant-client==1.9.2`, the server's version: a newer client (about 1.10 and later) serialises dense vectors over gRPC in a way the 1.9.2 server reads as empty (`expected dim: 384, got 0`). The client and the server have to be upgraded together. The server is reachable over gRPC only (`:6334`); the REST port is not published.

## Qdrant sync client in a thread pool

`db/qdrant.py` wraps the synchronous client and runs each call in the default executor (`run_in_executor`), so a slow vector search does not block the event loop of an async app. *(Inferred from the code: the module docstring only states "sync, wrapped in a threadpool for async".)* Neo4j uses the official async driver with a pool of 20 connections.

## Deployment choices

- **Zero-downtime release of the API only** (`scripts/deploy.sh`): `git pull --ff-only`, rebuild just the `fastapi` image, `docker compose up -d --no-deps fastapi`, wait for `/health`, prune images. Neo4j is not restarted.
- **Neo4j is never public.** The compose file publishes Bolt only on the private-network address of the host (`PRIVATE_BIND_IP`); data import from the ETL machine goes over that private network.
- **Memory is budgeted for 4 GB** (Neo4j heap 1 GB, page cache 512 MB, transactions capped at 128 MB each, 512 MB in total).
- **Backups** stop Neo4j for the time of a dump (`neo4j-admin database dump`), which is acceptable for a weekly job on a read-mostly service; backups older than 30 days are deleted.
- **The container runs as a non-root user** and the image has `curl` for the health check.

## The evaluation calls production code

`fastapi/scripts/eval_hybrid.py` imports and calls the real `_hybrid_rerank` instead of a re-implemented copy, so the measurement cannot drift away from what is deployed (the script's docstring cites the experience of a train/serve mismatch in another project). It also uses an independent signal (genre) rather than the signal the re-ranking optimises. See [evaluation.md](evaluation.md).

## Changed or dropped along the way

- **Wikidata keys → TMDB keys**, and the node type `Work`: a request for the hub type `work` now answers 400.
- **The old claim that `neo4j_loader.py` has a `--wipe` flag** was removed from the schema file; the documented procedure is to re-apply `schema_init.cypher` after any full reload.
- **Caddy** is mentioned in comments, the old runbook and the systemd unit, but the compose file has no Caddy service (the unused volumes `caddy_data` and `caddy_config` were removed); see [deployment.md](deployment.md#known-inconsistencies-in-the-deployment-files).
- **The APOC plugin jar** is no longer tracked in git: the image downloads it because of `NEO4J_PLUGINS` in the compose file.
