#!/usr/bin/env bash
# scripts/backup_neo4j.sh
#
# Еженедельный бэкап Neo4j через neo4j-admin database dump.
# Минутный даунтайм: контейнер останавливается на время дампа.
#
# Настройка cron (от root или пользователя с доступом к docker):
#   sudo crontab -e
#   0 3 * * 0  /opt/tvkg/vds1/scripts/backup_neo4j.sh >> /var/log/neo4j-backup.log 2>&1

set -euo pipefail

COMPOSE_DIR="/home/graph_series_backend"
BACKUP_DIR="/home/graph_series_backend/backups/neo4j"
KEEP_DAYS=30
DATE=$(date +%Y-%m-%d_%H%M)
ARCHIVE="${BACKUP_DIR}/neo4j_${DATE}.dump"

mkdir -p "${BACKUP_DIR}"

echo "[$(date)] ── Начало бэкапа Neo4j ──"

# Остановить только neo4j (fastapi и caddy получат 502, но ненадолго)
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" stop neo4j
echo "[$(date)] Neo4j остановлен"

# Дамп через neo4j-admin (запускаем в том же контейнере)
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" run --rm \
  --entrypoint neo4j-admin \
  neo4j \
  database dump neo4j --to-path=/import/backup.dump

# Копируем из тома в backup-директорию
docker run --rm \
  -v "${COMPOSE_DIR}/neo4j/import:/src" \
  -v "${BACKUP_DIR}:/dst" \
  alpine cp /src/backup.dump "/dst/neo4j_${DATE}.dump"

# Запускаем neo4j обратно
docker compose -f "${COMPOSE_DIR}/docker-compose.yml" start neo4j
echo "[$(date)] Neo4j запущен"

# Удаляем старые бэкапы
find "${BACKUP_DIR}" -name "*.dump" -mtime "+${KEEP_DAYS}" -delete
echo "[$(date)] Старые бэкапы удалены (старше ${KEEP_DAYS} дней)"

SIZE=$(du -sh "${ARCHIVE}" | cut -f1)
echo "[$(date)] ✔ Бэкап готов: ${ARCHIVE} (${SIZE})"
