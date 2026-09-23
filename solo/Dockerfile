# Образ упрощённого сервиса генерации QR-кодов.
#
# Контекст сборки — корень репозитория, а не каталог solo:
#   docker compose -f docker-compose.solo.yml build

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src:/app

# fonts-dejavu-core нужен Pillow, чтобы нарисовать кириллицу на
# картинке-заглушке: без шрифта текст ошибки был бы нечитаемым.
# curl нужен только для HEALTHCHECK.
RUN apt-get update \
    && apt-get install --no-install-recommends -y fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 nosbp

WORKDIR /app

# Сначала зависимости: пока requirements.txt не менялся, Docker берёт
# слой из кэша и не качает их заново.
COPY solo/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Платёжное ядро берётся из основного проекта целыми файлами: строка
# ГОСТ и отрисовка кода обязаны остаться теми же, что в полном сервисе.
# Перечисленные модули не зависят ни от базы, ни от хранилища — за этим
# следит тест tests/test_solo.py.
COPY src/nosbp/__init__.py src/nosbp/
COPY src/nosbp/core/__init__.py \
     src/nosbp/core/constants.py \
     src/nosbp/core/errors.py \
     src/nosbp/core/
COPY src/nosbp/payments/__init__.py \
     src/nosbp/payments/gost.py \
     src/nosbp/payments/placeholder.py \
     src/nosbp/payments/qr.py \
     src/nosbp/payments/requisites.py \
     src/nosbp/payments/schemas.py \
     src/nosbp/payments/

COPY solo ./solo

# Процесс не должен иметь прав root: если в приложении найдут дыру,
# из-под непривилегированного пользователя ей воспользоваться труднее.
USER nosbp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/health || exit 1

# --factory: приложение собирается при старте, когда переменные
# окружения уже на месте. Неверная настройка гасит контейнер сразу
# и с понятным сообщением, а не отвечает ошибками на каждый запрос.
CMD ["uvicorn", "solo.app:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000"]
