"""Точка входа FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from typing import Final
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from nosbp.admin.dependencies import build_templates
from nosbp.admin.routes import router as admin_router
from nosbp.core.config import Settings, get_settings
from nosbp.core.errors import AdminAuthError, NosbpError
from nosbp.core.logging import configure_logging
from nosbp.db.session import dispose_engine
from nosbp.payments.routes import router as payments_router
from nosbp.storage.logo_cache import LogoCache
from nosbp.storage.s3 import S3Storage

log = structlog.get_logger()

PACKAGE_NAME: Final[str] = "nosbp"
FALLBACK_VERSION: Final[str] = "0.0.0"

SEE_OTHER: Final = 303
"""Код перенаправления после неудачной проверки сессии."""

TITLE: Final[str] = "NoSBP"
DESCRIPTION: Final[str] = (
    "Генерация QR-кодов для оплаты по банковским реквизитам по ГОСТ Р 56042-2014."
)


def get_version() -> str:
    """Берёт версию из метаданных установленного пакета.

    Так номер версии живёт в одном месте — в pyproject.toml — и не
    расходится с тем, что показывает документация API.
    """
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        # Пакет запущен из исходников без установки — например, в редакторе.
        return FALLBACK_VERSION


def build_logo_cache(app: FastAPI, settings: Settings) -> LogoCache:
    """Собирает кэш логотипов поверх хранилища приложения.

    Хранилище может быть подставлено заранее — так тесты работают
    с памятью вместо сети.
    """
    if not hasattr(app.state, "storage"):
        app.state.storage = S3Storage(settings)
    return LogoCache(
        app.state.storage,
        ttl_seconds=settings.logo_cache_ttl_seconds,
        max_entries=settings.logo_cache_max_entries,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Готовит и освобождает ресурсы приложения.

    Схема базы данных здесь НЕ создаётся: за неё отвечает Alembic.
    Приложение, создающее таблицы само, рано или поздно разойдётся
    с миграциями, и на проде это обнаружится в худший момент.
    """
    settings = get_settings()
    app.state.logo_cache = build_logo_cache(app, settings)
    app.state.admin_templates = build_templates(settings)

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
        title=TITLE,
        description=DESCRIPTION,
        version=get_version(),
        lifespan=lifespan,
    )

    @app.exception_handler(AdminAuthError)
    async def handle_admin_auth(
        request: Request, exc: AdminAuthError
    ) -> RedirectResponse:
        """Отправляет на страницу входа вместо голого кода 401.

        Панель — это обычные страницы в браузере: администратор должен
        увидеть форму входа, а не JSON с ошибкой.
        """
        prefix = settings.admin_path_prefix.rstrip("/")
        query = urlencode({"err": exc.message})
        return RedirectResponse(f"{prefix}/login?{query}", status_code=SEE_OTHER)

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
    app.include_router(admin_router, prefix=settings.admin_path_prefix.rstrip("/"))
    return app


app = create_app()
