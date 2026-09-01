"""Точка входа FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from nosbp.core.config import get_settings
from nosbp.core.errors import NosbpError
from nosbp.core.logging import configure_logging
from nosbp.db.session import dispose_engine
from nosbp.payments.routes import router as payments_router
from nosbp.storage.logo_cache import LogoCache
from nosbp.storage.s3 import S3Storage

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Готовит и освобождает ресурсы приложения.

    Схема базы данных здесь НЕ создаётся: за неё отвечает Alembic.
    Приложение, создающее таблицы само, рано или поздно разойдётся
    с миграциями, и на проде это обнаружится в худший момент.
    """
    settings = get_settings()

    # Тесты подставляют своё хранилище до старта, чтобы не ходить в сеть.
    if not hasattr(app.state, "storage"):
        app.state.storage = S3Storage(settings)
    app.state.logo_cache = LogoCache(app.state.storage)

    log.info("service_started", environment=settings.environment)
    yield
    await dispose_engine()
    log.info("service_stopped")


def create_app() -> FastAPI:
    """Собирает приложение.

    Фабрика, а не глобальный объект: так тесты могут поднять отдельный
    экземпляр с другими настройками, не перезагружая модуль.
    """
    settings = get_settings()
    configure_logging(json_logs=not settings.is_local, log_level=settings.log_level)

    app = FastAPI(
        title="NoSBP",
        description=(
            "Генерация QR-кодов для оплаты по банковским реквизитам "
            "по ГОСТ Р 56042-2014."
        ),
        version="0.2.0",
        lifespan=lifespan,
    )

    @app.exception_handler(NosbpError)
    async def handle_domain_error(request: Request, exc: NosbpError) -> JSONResponse:
        """Превращает доменную ошибку в аккуратный JSON.

        GET-эндпоинт генерации QR перехватывает такие ошибки раньше и
        отдаёт картинку-заглушку — сюда попадает всё остальное.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code, "message": exc.message},
        )

    @app.get("/health", tags=["service"], summary="Проверка живости")
    async def health() -> dict[str, str]:
        """Отвечает, пока процесс жив. Используется Docker и мониторингом."""
        return {"status": "ok"}

    app.include_router(payments_router)
    return app


app = create_app()
