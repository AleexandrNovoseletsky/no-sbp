#!/bin/sh
# Резервная копия базы.
#
# Кладёт дамп в каталог BACKUP_DIR и удаляет копии старше KEEP_DAYS дней.
# Формат custom (-Fc), а не обычный SQL: он сжат, восстанавливается
# параллельно и позволяет вытащить одну таблицу, не разбирая весь файл.
#
# Запуск с хоста:
#     ./docker/backup.sh
#
# По расписанию — строка в crontab, каждый день в 4 утра:
#     0 4 * * * cd /opt/nosbp && ./docker/backup.sh >> /var/log/nosbp-backup.log 2>&1

set -eu

COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml}"
SERVICE="${SERVICE:-postgres}"
BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

# Переменные базы берутся из того же .env, что и compose.
if [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
fi
DB_USER="${POSTGRES_USER:-nosbp}"
DB_NAME="${POSTGRES_DB:-nosbp}"

STAMP="$(date +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/nosbp-${STAMP}.dump"

mkdir -p "$BACKUP_DIR"

echo "Снимаю копию базы ${DB_NAME} → ${TARGET}"
# Пишем во временный файл и переименовываем только после успеха: иначе
# оборванный дамп остался бы в каталоге и выглядел как рабочая копия.
$COMPOSE exec -T "$SERVICE" pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "${TARGET}.part"
mv "${TARGET}.part" "$TARGET"

SIZE="$(du -h "$TARGET" | cut -f1)"
echo "Готово: ${TARGET} (${SIZE})"

echo "Удаляю копии старше ${KEEP_DAYS} дней"
find "$BACKUP_DIR" -name 'nosbp-*.dump' -type f -mtime "+${KEEP_DAYS}" -print -delete

echo
echo "Копия лежит на том же сервере — это ещё не резервная копия."
echo "Отправьте её в другое место, например:"
echo "  rclone copy ${TARGET} remote:nosbp-backups/"
