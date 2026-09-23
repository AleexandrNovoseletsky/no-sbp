#!/bin/sh
# Восстановление базы из дампа.
#
#     ./docker/restore.sh backups/nosbp-20260902-040000.dump
#
# Восстановление УДАЛЯЕТ текущее содержимое базы: --clean сносит объекты
# перед созданием. Скрипт спрашивает подтверждение, потому что запустить
# его не на том сервере — самая дорогая ошибка из возможных.

set -eu

DUMP="${1:-}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "Укажите файл дампа: ./docker/restore.sh backups/nosbp-….dump" >&2
    exit 1
fi

COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml}"
SERVICE="${SERVICE:-postgres}"

if [ -f .env ]; then
    # shellcheck disable=SC1091
    . ./.env
fi
DB_USER="${POSTGRES_USER:-nosbp}"
DB_NAME="${POSTGRES_DB:-nosbp}"

echo "Файл:    $DUMP"
echo "База:    $DB_NAME"
echo
echo "Текущее содержимое базы будет ЗАМЕНЕНО."
printf 'Введите имя базы «%s» для подтверждения: ' "$DB_NAME"
read -r CONFIRM
if [ "$CONFIRM" != "$DB_NAME" ]; then
    echo "Отменено." >&2
    exit 1
fi

echo "Останавливаю приложение, чтобы оно не писало в базу во время замены"
$COMPOSE stop api || true

echo "Восстанавливаю"
# --clean --if-exists сносит старые объекты; --no-owner нужен, если дамп
# снят под другим пользователем, чем тот, под которым восстанавливаем.
$COMPOSE exec -T "$SERVICE" pg_restore \
    -U "$DB_USER" -d "$DB_NAME" \
    --clean --if-exists --no-owner --single-transaction < "$DUMP"

echo "Запускаю приложение"
$COMPOSE up -d api

echo
echo "Готово. Проверьте, что версия схемы совпадает с кодом:"
echo "  $COMPOSE exec api alembic current"
