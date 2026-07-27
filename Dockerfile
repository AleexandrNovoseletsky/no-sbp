FROM python:3.14-slim AS base

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN pip install --no-cache-dir poetry

COPY pyproject.toml poetry.lock ./
RUN poetry install --no-root --only main

COPY src ./src

EXPOSE 8000

CMD ["uvicorn", "nosbp.main:app", "--host", "0.0.0.0", "--port", "8000"]
