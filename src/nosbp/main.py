"""Точка входа FastAPI-приложения."""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.nosbp.core.logging import configure_logging
from src.nosbp.db.database import init_db
from src.nosbp.payments.routes import router as payments_router

ENVIRONMENT = os.getenv("ENVIRONMENT", "local")

configure_logging(
    json_logs=ENVIRONMENT != "local",
    log_level=os.getenv("LOG_LEVEL", "INFO"),
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Выполняется один раз при старте приложения, до приёма первого запроса
    await init_db()
    yield
    # Место для очистки ресурсов при остановке — пока не нужно


app = FastAPI(title="NOSBP Payments QR Service", lifespan=lifespan)
app.include_router(payments_router, prefix="/generate/qr")
