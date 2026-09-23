"""Точка входа FastAPI-приложения."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from nosbp.admin.dependencies import build_templates
from nosbp.admin.routes import router as admin_router
from nosbp.cabinet.dependencies import build_templates as build_cabinet_templates
from nosbp.cabinet.routes import router as cabinet_router
from nosbp.core.config import Settings, get_settings
from nosbp.core.constants import SERVICE_NAME
from nosbp.core.errors import AdminAuthError, CabinetAuthError, NosbpError
from nosbp.core.logging import configure_logging
from nosbp.core.middleware import configure_middleware
from nosbp.db.session import dispose_engine
from nosbp.mail.service import build_mailer
from nosbp.notifications.service import Notifier, build_channels
from nosbp.pay.dependencies import build_templates as build_pay_templates
from nosbp.pay.routes import router as pay_router
from nosbp.payments.routes import router as payments_router
from nosbp.storage.logo_cache import LogoCache
from nosbp.storage.s3 import S3Storage

log = structlog.get_logger()

WEB_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "admin" / "static"
"""Каталог со стилями и скриптами. Общий для панели и кабинета."""

PAY_STATIC_DIRECTORY: Final[Path] = Path(__file__).parent / "pay" / "static"
"""Каталог стилей публичной страницы оплаты."""

PACKAGE_NAME: Final[str] = "nosbp"
FALLBACK_VERSION: Final[str] = "0.0.0"

SEE_OTHER: Final = 303
"""Код перенаправления после неудачной проверки сессии."""

TITLE: Final[str] = SERVICE_NAME
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


def prepare_state(app: FastAPI, settings: Settings) -> None:
    """Наполняет состояние приложения общими ресурсами.

    Вынесено из обработчика жизненного цикла, чтобы тесты, поднимающие
    приложение без него, получали тот же набор ресурсов и не расходились
    с рабочей конфигурацией по мере добавления новых.
    """
    app.state.logo_cache = build_logo_cache(app, settings)
    app.state.admin_templates = build_templates(settings)
    app.state.cabinet_templates = build_cabinet_templates(settings)
    app.state.pay_templates = build_pay_templates(settings)
    app.state.notifier = Notifier(build_channels(settings))
    app.state.mailer = build_mailer(settings)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Готовит и освобождает ресурсы приложения.

    Схема базы данных здесь не создаётся: за неё отвечает Alembic.
    Приложение, создающее таблицы самостоятельно, со временем расходится
    с историей миграций.
    """
    settings: Settings = app.state.settings
    prepare_state(app, settings)

    log.info(
        "service_started",
        environment=settings.environment,
        notifications=app.state.notifier.is_configured,
        mail=app.state.mailer.is_configured,
        payment_page=settings.payment_page_enabled,
    )
    yield
    await dispose_engine()
    log.info("service_stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собирает приложение.

    :param settings: конфигурация. По умолчанию берётся из окружения;
        явная передача используется в тестах.
    """
    settings = settings or get_settings()
    configure_logging(json_logs=not settings.is_local, log_level=settings.log_level)

    app = FastAPI(
        title=TITLE,
        description=DESCRIPTION,
        version=get_version(),
        lifespan=lifespan,
        # В рабочем окружении интерактивная документация не отдаётся:
        # она раскрывает состав и параметры эндпоинтов.
        docs_url="/docs" if settings.show_api_docs else None,
        redoc_url="/redoc" if settings.show_api_docs else None,
        openapi_url="/openapi.json" if settings.show_api_docs else None,
    )
    app.state.settings = settings
    configure_middleware(app, settings)

    # Зависимости обращаются к настройкам через get_settings; подмена
    # гарантирует, что маршруты видят ту же конфигурацию, что и фабрика.
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(AdminAuthError)
    async def handle_admin_auth(
        request: Request, exc: AdminAuthError
    ) -> RedirectResponse:
        """Отправляет на страницу входа вместо голого кода 401.

        Панель — это обычные страницы в браузере: администратор должен
        увидеть форму входа, а не JSON с ошибкой.
        """
        query = urlencode({"err": exc.message})
        return RedirectResponse(
            f"{settings.admin_prefix}/login?{query}", status_code=SEE_OTHER
        )

    @app.exception_handler(CabinetAuthError)
    async def handle_cabinet_auth(
        request: Request, exc: CabinetAuthError
    ) -> RedirectResponse:
        """Отправляет на страницу входа в кабинет вместо кода 401."""
        query = urlencode({"err": exc.message})
        return RedirectResponse(
            f"{settings.cabinet_prefix}/login?{query}", status_code=SEE_OTHER
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
    app.include_router(admin_router, prefix=settings.admin_prefix)
    app.include_router(cabinet_router, prefix=settings.cabinet_prefix)

    # Страница оплаты раскрывает реквизиты получателя всем, у кого есть
    # ссылка. Пока она не нужна, маршруты не регистрируются вовсе:
    # выключенная настройка означает честный 404, а не скрытую страницу.
    if settings.payment_page_enabled:
        app.mount(
            f"{settings.payment_prefix}/static",
            StaticFiles(directory=PAY_STATIC_DIRECTORY),
            name="pay-static",
        )
        app.include_router(pay_router, prefix=settings.payment_prefix)

    # Оформление у панели и кабинета общее, поэтому каталог статики один
    # и монтируется по обоим адресам.
    app.mount(
        f"{settings.admin_prefix}/static",
        StaticFiles(directory=WEB_STATIC_DIRECTORY),
        name="admin-static",
    )
    app.mount(
        f"{settings.cabinet_prefix}/static",
        StaticFiles(directory=WEB_STATIC_DIRECTORY),
        name="cabinet-static",
    )
    return app


app = create_app()
