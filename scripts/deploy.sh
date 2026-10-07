#!/usr/bin/env bash
# scripts/deploy.sh
#
# Вызывается из GitHub Actions (SSH на VDS1) для zero-downtime деплоя FastAPI.
# Neo4j и Caddy обновляются отдельно, вручную (редкое событие).
#
# Аргументы:
#   $1 — git ref / тег (необязательно, для логов)

set -euo pipefail

COMPOSE_DIR="/home/graph_series_backend"
REF="${1:-HEAD}"

echo "[$(date)] ── Deploy VDS1 (ref: ${REF}) ──"

cd "${COMPOSE_DIR}"

# Обновляем код (если репо смонтировано)
if [ -d ".git" ]; then
  git pull --ff-only
fi

# Пересобираем только fastapi (без остановки neo4j и caddy)
docker compose build fastapi

# Rolling restart: поднимаем новый контейнер, старый уходит
docker compose up -d --no-deps fastapi

# Ждём healthcheck
echo "[$(date)] Ожидаем /health ..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/health > /dev/null; then
    echo "[$(date)] ✔ FastAPI healthy (попытка ${i})"
    break
  fi
  sleep 2
done

# Чистим старые образы
docker image prune -f

echo "[$(date)] ✔ Deploy завершён"
