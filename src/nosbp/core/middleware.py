"""Промежуточные обработчики HTTP-запросов."""

import ipaddress
from collections.abc import Awaitable, Callable
from typing import Final

import structlog
from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from nosbp.core.config import Settings

log = structlog.get_logger()

CallNext = Callable[[Request], Awaitable[Response]]

FORWARDED_FOR_HEADER: Final[str] = "x-forwarded-for"

CONTENT_SECURITY_POLICY: Final[str] = (
    "default-src 'none'; "
    "img-src 'self' data:; "
    "style-src 'self'; "
    "script-src 'self'; "
    "form-action 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)
"""Политика безопасности контента.

Панель не подключает внешних ресурсов, а её стили и скрипты вынесены
в отдельные файлы, поэтому встроенные обработчики и стили запрещены
целиком: внедрённый на страницу код не сможет выполниться.
"""

SECURITY_HEADERS: Final[dict[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}

HSTS_HEADER: Final[str] = "Strict-Transport-Security"
HSTS_VALUE: Final[str] = "max-age=63072000; includeSubDomains"

FORBIDDEN_MESSAGE: Final[str] = "Not Found"
"""Ответ на запрос к панели с неразрешённого адреса.

Совпадает с ответом на несуществующий путь, чтобы не подтверждать
наличие панели по этому адресу.
"""


def client_address(request: Request) -> str:
    """Определяет адрес клиента с учётом обратного прокси.

    Заголовку ``X-Forwarded-For`` можно доверять только при условии, что
    прокси его перезаписывает, а сам сервис недоступен напрямую. Оба
    требования описаны в инструкции по развёртыванию.
    """
    forwarded = request.headers.get(FORWARDED_FOR_HEADER, "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Добавляет заголовки безопасности ко всем ответам."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        super().__init__(app)
        self._hsts = hsts

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        if self._hsts:
            response.headers.setdefault(HSTS_HEADER, HSTS_VALUE)
        return response


class AdminNetworkMiddleware(BaseHTTPMiddleware):
    """Ограничивает доступ к панели управления списком сетей.

    Основное ограничение задаётся на обратном прокси; эта проверка
    дублирует его на случай, если сервис окажется доступен в обход прокси.
    При пустом списке сетей проверка не выполняется.
    """

    def __init__(
        self, app: ASGIApp, *, prefix: str, networks: tuple[object, ...]
    ) -> None:
        super().__init__(app)
        self._prefix = prefix
        self._networks = networks

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if not self._networks or not request.url.path.startswith(self._prefix):
            return await call_next(request)

        address = client_address(request)
        if not self._is_allowed(address):
            log.warning("admin_access_denied", address=address, path=request.url.path)
            return PlainTextResponse(FORBIDDEN_MESSAGE, status_code=404)

        return await call_next(request)

    def _is_allowed(self, address: str) -> bool:
        """Проверяет принадлежность адреса разрешённым сетям."""
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(parsed in network for network in self._networks)  # type: ignore[operator]


def configure_middleware(app: ASGIApp, settings: Settings) -> None:
    """Подключает промежуточные обработчики в порядке применения."""
    app.add_middleware(  # type: ignore[attr-defined]
        AdminNetworkMiddleware,
        prefix=settings.admin_prefix,
        networks=settings.admin_networks,
    )
    app.add_middleware(  # type: ignore[attr-defined]
        SecurityHeadersMiddleware,
        hsts=settings.is_production,
    )
