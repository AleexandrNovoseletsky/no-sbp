# Многоступенчатая сборка: зависимости ставятся один раз и переиспользуются,
# поэтому пересборка после правки кода занимает секунды, а не минуты.

FROM python:3.13-slim AS builder

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry>=2.0,<3.0"

# Сначала только манифесты: пока они не менялись, Docker берёт слой
# с зависимостями из кэша и не качает их заново.
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root

# Теперь код и установка самого пакета — ради консольной команды nosbp.
COPY src ./src
RUN poetry install --only main


FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# fonts-dejavu-core нужен Pillow, чтобы нарисовать кириллицу на
# картинке-заглушке. Без шрифта текст ошибки был бы нечитаемым.
# curl нужен только для HEALTHCHECK.
RUN apt-get update \
    && apt-get install --no-install-recommends -y fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 nosbp

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml alembic.ini README.md ./
COPY alembic ./alembic
COPY src ./src
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && chown -R nosbp:nosbp /app

# Процесс не должен иметь прав root: если в приложении найдут дыру,
# из-под непривилегированного пользователя ей воспользоваться труднее.
USER nosbp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "nosbp.main:app", "--host", "0.0.0.0", "--port", "8000"]
