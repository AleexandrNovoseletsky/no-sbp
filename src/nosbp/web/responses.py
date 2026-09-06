"""Общие элементы веб-интерфейсов.

Панель управления и личный кабинет устроены одинаково: серверные формы,
перенаправление после успешной операции и повторная отрисовка страницы
с сообщением при ошибке. Всё, что от этого не зависит, собрано здесь.
"""

from collections.abc import Mapping
from typing import Final, Protocol
from urllib.parse import urlencode

from fastapi import Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from nosbp.core.config import Settings
from nosbp.core.errors import NosbpError

SEE_OTHER: Final = 303
"""Код ответа после успешной обработки формы.

Перенаправление после POST исключает повторное выполнение операции при
обновлении страницы.
"""

NOT_MODIFIED: Final = 304
SECONDS_PER_HOUR: Final = 60 * 60

FormValues = dict[str, str | bool]
"""Значения полей формы.

Шаблоны читают значения отсюда, а не из модели: при ошибке валидации
форма возвращается заполненной введёнными данными.
"""


class Viewer(Protocol):
    """Тот, кто открыл страницу: администратор или заказчик."""

    @property
    def csrf_token(self) -> str: ...

    @property
    def email(self) -> str: ...

    @property
    def display_name(self) -> str: ...


def page(
    templates: Jinja2Templates,
    request: Request,
    name: str,
    settings: Settings,
    *,
    viewer: Viewer | None = None,
    err: str = "",
    **context: object,
) -> Response:
    """Отрисовывает страницу, добавив общие для всех шаблонов значения.

    Токен CSRF подставляется автоматически, чтобы его нельзя было
    пропустить при добавлении нового шаблона.
    """
    return templates.TemplateResponse(
        request,
        name,
        {
            "settings": settings,
            "viewer": viewer,
            "csrf": viewer.csrf_token if viewer is not None else "",
            "ok": request.query_params.get("ok", ""),
            "err": err or request.query_params.get("err", ""),
            **context,
        },
    )


def redirect(prefix: str, path: str, **flash: str) -> RedirectResponse:
    """Перенаправляет внутрь интерфейса, передав сообщение через адрес.

    :param prefix: путь интерфейса, например ``/console``.
    :param path: путь внутри интерфейса, начиная со слэша.
    :param flash: параметры ``ok`` и ``err`` для показа на целевой странице.
    """
    query = urlencode({key: value for key, value in flash.items() if value})
    target = f"{prefix}{path}"
    return RedirectResponse(f"{target}?{query}" if query else target, SEE_OTHER)


def error_text(error: Exception) -> str:
    """Достаёт человеческий текст из доменной ошибки или из ValueError."""
    return error.message if isinstance(error, NosbpError) else str(error)


async def reload_after_rollback(db: AsyncSession, *instances: object) -> None:
    """Перечитывает объекты после отката транзакции.

    Откат помечает загруженные объекты устаревшими. Шаблон, дойдя до
    такого объекта, попытался бы обратиться к базе во время отрисовки,
    чего синхронный Jinja сделать не может.
    """
    for instance in instances:
        if instance is not None:
            await db.refresh(instance)


def checkbox(value: str | None) -> bool:
    """Браузер присылает отмеченную галочку строкой, а снятую — ничем."""
    return value is not None


def template_globals(prefix_name: str, prefix: str) -> Mapping[str, object]:
    """Значения, общие для всех шаблонов интерфейса.

    :param prefix_name: имя переменной с путём интерфейса в шаблонах.
    :param prefix: сам путь, например ``/cabinet``.
    """
    from nosbp.admin.stats import format_fee_percent
    from nosbp.core.money import format_roubles, roubles_input
    from nosbp.core.security import CSRF_FIELD_NAME

    return {
        prefix_name: prefix,
        "csrf_field": CSRF_FIELD_NAME,
        "format_roubles": format_roubles,
        "format_fee_percent": format_fee_percent,
        "roubles_input": roubles_input,
    }
