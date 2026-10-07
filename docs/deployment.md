# Deployment

How the backend is deployed, rewritten from the original (Russian) `RUNBOOK.md` and checked against `docker-compose.yml`, `fastapi/Dockerfile`, `scripts/` and `.github/workflows/ci-vds1.yml`. **Nothing in this file was run in this environment** (no Docker, no private network, no second server); the steps describe what the files do. Values in angle brackets are for you to supply.

**Contents:** [Topology](#topology) · [Services](#services-in-docker-composeyml) · [Configuration](#configuration) · [First-time setup](#first-time-setup) · [Loading the data](#loading-the-data) · [Releases](#releases-and-ci) · [Regular operations](#regular-operations) · [Resources](#resources) · [Known inconsistencies in the deployment files](#known-inconsistencies-in-the-deployment-files)

## Topology

Two servers on a private network (Tailscale in the original setup):

| Server | Runs | Source |
|---|---|---|
| **API server** ("VDS1" in the scripts) | Neo4j and the FastAPI app (Docker Compose), a reverse proxy with TLS in front | this repository |
| **web server** ("VDS2") | Qdrant 1.9.2 (gRPC `:6334`), the embedding service (`:8004`), the web app | [`graph-series_ml`](https://github.com/GKatzer/graph-series_ml) |

The API server reaches Qdrant and the embedding service over the private network. Neo4j is not published to the internet: its Bolt port is bound to the host's private-network address only, so the ETL machine can load data over the same network.

## Services in `docker-compose.yml`

| Service | Image | Ports | Notes |
|---|---|---|---|
| `neo4j` | `neo4j:5-community`, APOC plugin downloaded by the image (`NEO4J_PLUGINS`) | Bolt `7687` published on `${PRIVATE_BIND_IP}` only; `expose` 7687 and 7474 inside the Docker network | heap 512 MB initial, 1 GB max, page cache 512 MB; data in `./neo4j/data`, logs, import and plugins mounted; health check with `cypher-shell` |
| `fastapi` | built from `fastapi/Dockerfile` (`python:3.12-slim`, `curl`, non-root user, 2 uvicorn workers, log config `app/log_config.json`) | `127.0.0.1:8000` and `${PRIVATE_BIND_IP}:8000` | `env_file: .env`; waits for Neo4j to be healthy; health check `curl -f http://localhost:8000/health` |

The host's private-network address comes from the variable `PRIVATE_BIND_IP` (read from `.env`); compose refuses to start while it is unset, so **add it to the server's existing `.env` before the next deploy**.

## Configuration

Read by the app (`fastapi/app/config.py`, `fastapi/app/services/embedder.py`); set in `.env` (not committed; template `.env.example`) or in the compose `environment` block.

| Variable | Meaning | Default | Required |
|---|---|---|---|
| `NEO4J_URI` | Bolt address | `bolt://neo4j:7687` | no (compose sets it) |
| `NEO4J_USER` | Neo4j user | `neo4j` | no |
| `NEO4J_PASSWORD` | Neo4j password (also used by compose to start Neo4j) | none | **yes** |
| `QDRANT_HOST` | Qdrant host (compose fills it from `QDRANT_TAILSCALE_HOST`) | none | **yes** |
| `QDRANT_PORT` | REST port, only needed by the client constructor, not used | `6333` | no |
| `QDRANT_GRPC_PORT` | gRPC port | `6334` | no |
| `QDRANT_PREFER_GRPC` | use gRPC | `true` | no |
| `QDRANT_COLLECTION` | collection name | `graph-series` | no |
| `EMBED_SERVICE_URL` | URL of the embedding service's `/embed` | `http://host.docker.internal:8004/embed` | in practice yes, the default only works when the service is on the Docker host |
| `CORS_ORIGINS_RAW` | allowed origins, comma separated | the project's own origins and `http://localhost:3000` | set it for your site |
| `APP_ENV` | `production` hides `/docs` | `production` | no |
| `API_SECRET_KEY` | declared in settings, **not used anywhere in the code** | empty | no |

Compose-only: `QDRANT_TAILSCALE_HOST` (address of the web server on the private network, fills `QDRANT_HOST`) and `PRIVATE_BIND_IP` (address of this host on the private network, on which Neo4j and the API are published; required). The old `.env.example` also listed `API_DOMAIN` (used by nothing in the repository, a leftover of the Caddy plan).

## First-time setup

1. **Prerequisites** on the API server: Docker Engine with the Compose plugin, and the private network (Tailscale: install, `tailscale up`, note `tailscale ip -4`).
2. **Clone** the repository into `<install-dir>` and `cd` there.
3. **Create `.env`** from `.env.example` and fill it in (see the table; do not forget `PRIVATE_BIND_IP`). Use a random password of at least 16 characters.
4. **Create the Neo4j directories:** `mkdir -p neo4j/{data,logs,import,plugins}`. The original runbook also loosened the permissions of `neo4j/data` and `neo4j/logs` (`chmod 777`, because Neo4j writes as its own uid); choose something tighter if you can.
5. **Start Neo4j** and wait about 30 to 60 seconds for the health check: `docker compose up -d neo4j`.
6. **Apply the schema** (idempotent):
   ```bash
   docker compose exec -T neo4j sh -c 'cypher-shell -u neo4j -p "${NEO4J_AUTH#neo4j/}"' < scripts/schema_init.cypher
   ```
   The single quotes matter: `${NEO4J_AUTH#neo4j/}` has to be expanded inside the container. The script creates seven uniqueness constraints, the two full-text indexes (`series_name_idx`, `person_name_idx`) and a range index on `start_year`, then prints `SHOW INDEXES`. Full-text indexes start in state `POPULATING`; requests that use them fail until they are `ONLINE`.
7. **Load the data** (next section).
8. **Start the API:** `docker compose up -d fastapi`; check `curl http://localhost:8000/health` and `/health/full` (the latter answers 503 until both full-text indexes are `ONLINE`).
9. **Reverse proxy and TLS** in front of port 8000: forward `/api/*` and `/health` to it; not part of this repository.
10. **Autostart (optional):** `scripts/tvkg-vds1.service` is a `oneshot` systemd unit that runs `docker compose up -d --remove-orphans` from the install directory and `docker compose down --timeout 60` on stop. Copy it to `/etc/systemd/system/`, set `WorkingDirectory`, then `daemon-reload` and `enable`.
11. **Weekly backup:** add `scripts/backup_neo4j.sh` to root's crontab, for example `0 3 * * 0`.

## Loading the data

The data comes from [`graph-series_ETL`](https://github.com/GKatzer/graph-series_ETL), run on another machine on the private network:

```bash
python qdrant_loader.py --recreate    # collection graph-series on the web server (gRPC :6334), about 211k points
python neo4j_loader.py                # series with at least 2 votes (about 56k); --min-votes N changes the threshold
```

Neo4j accepts Bolt only inside the Docker network and on the private address, so the loader can connect to `bolt://<api-server-private-ip>:7687`. **After a full wipe and reload of the graph, apply `scripts/schema_init.cypher` again**: the wipe removes the constraints and indexes and the loader does not recreate them, so without it the full-text endpoints (`structural` search, person search, `/api/series?name=`) answer HTTP 500. This is what happened on 2026-10-04 on the public deployment.

## Releases and CI

`.github/workflows/ci-vds1.yml` runs on pushes and pull requests to `master` that touch `fastapi/**`:

1. `ruff check app/`, `mypy app/ --ignore-missing-imports`, `pytest tests/ -v` with `NEO4J_PASSWORD=ci` and `QDRANT_HOST=localhost` (Python 3.12);
2. on a push to `master`, a `deploy` job on a **self-hosted runner** on the API server runs `bash <install-dir>/scripts/deploy.sh <sha>` (the install directory is fixed inside the script and the workflow).

`scripts/deploy.sh` does `git pull --ff-only`, rebuilds only the `fastapi` image, restarts it with `docker compose up -d --no-deps fastapi`, polls `/health` up to 30 times at 2 s intervals, and prunes dangling images. Neo4j is left running.

The three CI steps were run locally (not on GitHub) in a clean environment on 2026-10-04: `ruff` passes, `mypy` reports no issues in 17 source files, `pytest` passes 43 tests (30 before the changes of the same day). No GitHub Actions run of this workflow has been observed.

## Regular operations

- **Restart the API only:** `docker compose up -d --no-deps fastapi`.
- **Backup now:** `bash scripts/backup_neo4j.sh` stops Neo4j, dumps the database with `neo4j-admin database dump` into `neo4j/import/backup.dump`, copies it to `backups/neo4j/neo4j_<date>.dump`, starts Neo4j again and removes dumps older than 30 days. The API is unavailable for the duration.
- **Slow queries:** the Neo4j query log is on (`db.logs.query.threshold=1000ms`); `CALL db.listQueries()` lists running queries.
- **Memory:** `docker stats --no-stream`.
- **Update Neo4j:** `docker compose pull neo4j && docker compose up -d neo4j`.

## Resources

Planned for a 4 GB server (estimates from the original runbook, not measured here): Neo4j about 1.5 GB (1 GB heap and 512 MB page cache), FastAPI with two workers about 150 MB, OS and Docker about 500 MB; roughly 2.2 GB in total, leaving about 1.8 GB.

## Known inconsistencies in the deployment files

Found while checking the files against each other. Items marked *fixed* were changed on 2026-10-04.

1. **No reverse-proxy service.** The old runbook, comments and `tvkg-vds1.service` mention Caddy, but there is no Caddy service and no Caddyfile in the repository. *Fixed:* the unused volumes `caddy_data` and `caddy_config` were removed from the compose file.
2. **Private addresses were hard-coded** in `docker-compose.yml` (the host's own address in two port mappings and the web server's in an `extra_hosts` alias that nothing used). *Fixed:* the host address is `PRIVATE_BIND_IP`; the unused alias was removed.
3. **`.env.example` was incomplete** (no `EMBED_SERVICE_URL`, `QDRANT_GRPC_PORT`, `CORS_ORIGINS_RAW`; it listed `API_DOMAIN` and `API_SECRET_KEY`, which no code uses). *Fixed:* it matches the code.
4. **Embedding service port.** The old runbook's example `EMBED_SERVICE_URL` used port 8001; the code default and the inference service's published port are 8004.
5. **Old runbook errors** (the file is gone): a clone URL with a typo and the wrong organisation, and "Neo4j only inside the Docker network" while Bolt is also published on the private address.
6. **Paths.** `backup_neo4j.sh` suggests a cron path under `/opt/tvkg/vds1/`, while every other file uses the install directory the compose file lives in.
7. **`docs_url`** is disabled in production, so the OpenAPI page cannot be used to explore the deployed API; this document and `api.md` replace it.
8. **The APOC jar** was tracked in git. *Fixed:* it is untracked and ignored; the image downloads the plugin (`NEO4J_PLUGINS` was already set). The file stays on the server's disk.
